"""The tour agent — designs a guided reading tour, connected by the real import graph.

A newcomer's hardest problem is knowing WHERE the app starts and HOW the pieces
connect. The tour answers that in two steps:

  1. AGENT (`_run_tour_agent`): Claude explores the indexed repo with the 9
     read-only tools, confirms the real entry point by reading it, and returns
     chapters + stops (path, reason, insight) designed for THIS project.
     `_validate_tour_spec` keeps only stops that exist in the index AND that the
     agent actually saw through a tool — invented paths are dropped.

  2. GRAPH (`_assemble_tour`): for the chosen stops, Python adds what must be
     exact, not model-written: each stop's connections ("uses" / "used_by" with
     the real imported names), the mini-map flow graph and the wiring diagram —
     all from the static import graph (`cortex/graph/imports.py`).

Cached to <index>/<repo>/tour.json, because an agent run takes about a minute.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Optional

from cortex.agents import loop
from cortex.agents.tools import ToolExecutor
from cortex.config import Settings, get_settings
from cortex.graph.imports import bfs_depth, build_import_graph, is_source_file, text_by_path
from cortex.graph.wiring import _dirname, _imported_names, _role, _symbols, build_wiring
from cortex.index.store import get_store, slugify
from cortex.storage.annotations import load_annotations
from cortex.trace import logger

TOUR_MAX_TURNS = 30

TOUR_SYSTEM_PROMPT = f"""\
You are a senior engineer designing a GUIDED READING TOUR of a codebase for a
teammate who has never seen it. You have read-only tools over the indexed repo.
Investigate first, then design the tour around how THIS project is actually built.

HOW TO WORK:
1. Orient: look at the file layout, the dependency manifest(s) and the README.
2. Find the REAL entry point (what runs first: main/server/bootstrap/CLI) and
   confirm it by reading it. Do not guess from the filename alone.
3. Follow the flow outward from the entry point: see what it imports and calls,
   and read the files the app really depends on.
4. Design chapters that fit this project's architecture — NOT a generic
   template. A CLI tool, an Angular SPA, a REST API and a data pipeline should get
   very different chapters. Name chapters in this project's own vocabulary.
5. Pick the files that matter most for understanding, in the order a newcomer
   should read them. Skip boilerplate, generated code, tests and trivial files.
   Only include a file as a stop if you actually opened it.

{loop.AGENT_SAFETY}
When done investigating, return the tour. The first stop of the first chapter
must be the entry point.
"""

# The shape of the tour. The SDK makes the agent's final answer match it
# (structured output); the descriptions tell the agent what to put in each field.
TOUR_SCHEMA = {
    "type": "object",
    "required": ["overview", "entry_point", "chapters"],
    "properties": {
        "overview": {"type": "string",
                     "description": "2-3 sentences: what this project is and how it is organised"},
        "entry_point": {"type": "string",
                        "description": "repo-relative path of the file where execution starts"},
        "chapters": {"type": "array", "items": {
            "type": "object",
            "required": ["title", "why", "stops"],
            "properties": {
                "title": {"type": "string", "description": "chapter name in this project's terms"},
                "why": {"type": "string", "description": "one sentence: what the reader learns here"},
                "stops": {"type": "array", "items": {
                    "type": "object",
                    "required": ["path", "reason", "insight"],
                    "properties": {
                        "path": {"type": "string", "description": "repo-relative path of a file you opened"},
                        "reason": {"type": "string",
                                   "description": "why it is on the tour at this point, under 20 words"},
                        "insight": {"type": "string",
                                    "description": "what it does and why it matters, naming its key "
                                                   "functions/classes, under 30 words"},
                    }}},
            }}},
    },
}


def _tour_prompt(namespace: str, n_source_files: int, min_stops: int, max_stops: int) -> str:
    return (
        f"Repository: {namespace} ({n_source_files} indexed source files).\n"
        f"Design the guided tour. Choose as many stops as this repo genuinely needs — "
        f"roughly one per 7 source files — between {min_stops} and {max_stops} stops "
        f"in total, grouped into 2-6 chapters. Investigate with the tools first."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def build_tour(namespace: str, *, settings: Optional[Settings] = None) -> dict:
    """Build the guided tour for an indexed repo (agent + import graph).

    Raises ValueError if the namespace isn't indexed, loop.AgentUnavailableError
    if Claude can't be reached, and loop.AgentFailedError if the tour is unusable.
    """
    settings = settings or get_settings()
    store = get_store(settings)
    ns = slugify(namespace)
    if not store.exists(ns):
        raise ValueError(f"namespace not indexed: {ns}. Ingest the repo first.")
    docs = store._load_chunk_docs(ns)
    if not docs:
        raise ValueError(f"no indexed content for namespace: {ns}")

    texts = text_by_path(docs)
    source_files = [p for p in texts if is_source_file(p)]

    spec = _run_tour_agent(ns, store, texts, source_files, settings)
    out_adj, indeg = build_import_graph(source_files, texts)
    return _assemble_tour(ns, store, texts, out_adj, indeg, spec["entries"],
                          spec["chapters"], load_annotations(ns, settings=settings),
                          settings, overview=spec["overview"])


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: the agent designs the tour
# ─────────────────────────────────────────────────────────────────────────────

def _run_tour_agent(ns: str, store, texts: dict[str, str], source_files: list[str],
                    settings: Settings) -> dict:
    """Run the tour agent and return a validated spec."""
    executor = ToolExecutor(namespace=ns, store=store)
    prompt = _tour_prompt(ns, len(source_files), settings.tour_min_stops, settings.tour_max_stops)
    tour, errors = {}, []
    for _attempt in range(2):  # one retry on a transient failure
        try:
            tour, _turns = loop.run_tool_agent(
                executor, TOUR_SYSTEM_PROMPT, prompt, max_turns=TOUR_MAX_TURNS,
                settings=settings, errors=errors, output_schema=TOUR_SCHEMA)
            break
        except loop.AgentUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc)[:200])

    spec = _validate_tour_spec(tour, set(texts) - loop.PSEUDO_PATHS,
                               executor.used_paths, settings.tour_max_stops)
    if spec is None:
        logger.warning("tour_agent_failed namespace=%s errors=%s", ns, errors[-2:])
        raise loop.AgentFailedError(
            "the tour agent did not return a usable tour"
            + (f" ({errors[-1]})" if errors else "") + " — try again")
    logger.info("tour_agent_done namespace=%s stops=%d tool_calls=%d", ns,
                sum(len(c["stops"]) for c in spec["chapters"]), len(executor.call_log))
    return spec


def _validate_tour_spec(obj: dict, known: set[str], seen: set[str], max_stops: int) -> Optional[dict]:
    """Keep only stops whose path is in the index AND that the agent actually saw
    through a tool (`seen` = executor.used_paths). Invented or unseen paths are
    dropped; fewer than 2 surviving stops means the tour is unusable (None).
    The SDK already guarantees the shape (TOUR_SCHEMA); {} means no answer."""
    used: set[str] = set()
    chapters: list[dict] = []
    for ch in obj.get("chapters", []):
        stops = []
        for st in ch["stops"]:
            if len(used) >= max_stops:
                break
            p = loop.resolve_indexed_path(st["path"], known)
            if not p or p in used or p not in seen:
                continue
            used.add(p)
            stops.append({"path": p, "reason": st["reason"][:200], "insight": st["insight"][:300]})
        if stops:
            chapters.append({"title": ch["title"][:80] or "Chapter", "why": ch["why"][:240],
                             "stops": stops})
    if len(used) < min(2, len(known)):
        return None
    entry = loop.resolve_indexed_path(obj.get("entry_point", ""), known)
    if not entry or entry not in used:
        entry = chapters[0]["stops"][0]["path"]
    return {"overview": obj.get("overview", "")[:800], "entries": [entry], "chapters": chapters}


# ─────────────────────────────────────────────────────────────────────────────
# Step 2: connect the chosen stops with the real import graph
# ─────────────────────────────────────────────────────────────────────────────

def _annotation_for(annotations: list[dict], path: str) -> str:
    for a in annotations:
        if a.get("file") == path and a.get("answer"):
            return str(a["answer"])[:400]
    return ""


def _assemble_tour(ns: str, store, texts: dict[str, str], out_adj: dict[str, list[str]],
                   indeg: Counter, entries: list[str], chapters_spec: list[dict],
                   annotations: list[dict], settings: Settings, *, overview: str = "") -> dict:
    """Turn the agent's chapter/stop spec into a TourResponse dict: stop metadata
    from the index, plus connections, the flow mini-map and the wiring diagram —
    all derived from the real import graph, so nothing in this step is
    model-generated."""
    entry_set = set(entries)
    depth = bfs_depth(entries, out_adj, indeg)
    out_chapters: list[dict] = []
    all_paths: list[str] = []
    chapter_by_path: dict[str, str] = {}
    for ch in chapters_spec:
        stops = []
        for st in ch["stops"]:
            p = st["path"]
            fc = store.first_chunk(ns, p)
            if not fc:
                continue
            m = fc.get("metadata", {})
            stops.append({
                "path": p, "symbol": m.get("symbol", ""), "language": m.get("language", ""),
                "line_start": int(m.get("line_start", 0)), "line_end": int(m.get("line_end", 0)),
                "excerpt": (fc.get("text", "") or "")[:600], "depth": depth.get(p, 99),
                "imports": out_adj.get(p, [])[:6],
                "reason": st.get("reason", ""), "insight": st.get("insight", ""),
                "note": _annotation_for(annotations, p), "is_entry": p in entry_set,
            })
            all_paths.append(p)
            chapter_by_path[p] = ch["title"]
        if stops:
            out_chapters.append({"title": ch["title"], "why": ch.get("why", ""), "stops": stops})

    # ---- branching data: how the SELECTED stops connect to each other --------
    # Each stop advertises the stops it imports ("uses") and the stops that
    # import it ("used_by"), labelled with the real symbol names crossing that
    # edge, so the UI can offer "this connects to X and Y — which next?".
    stop_set = set(all_paths)
    incoming: dict[str, list[str]] = {p: [] for p in all_paths}
    for src in all_paths:
        for dst in out_adj.get(src, []):
            if dst in stop_set and dst != src:
                incoming[dst].append(src)

    def _connections_for(p: str) -> list[dict]:
        conns: list[dict] = []
        seen: set = set()
        for dst in out_adj.get(p, []):        # this file USES dst
            if dst in stop_set and dst != p and dst not in seen:
                seen.add(dst)
                conns.append({
                    "path": dst, "direction": "uses",
                    "symbols": _imported_names(texts.get(p, ""), dst),
                    "chapter": chapter_by_path.get(dst, ""), "is_entry": dst in entry_set,
                })
        for src in incoming.get(p, []):       # src USES this file (used_by)
            if src != p and src not in seen:
                seen.add(src)
                conns.append({
                    "path": src, "direction": "used_by",
                    "symbols": _imported_names(texts.get(src, ""), p),
                    "chapter": chapter_by_path.get(src, ""), "is_entry": src in entry_set,
                })
        return conns[:8]

    for ch in out_chapters:
        for s in ch["stops"]:
            s["connections"] = _connections_for(s["path"])

    # A compact directed graph over just the tour stops, for the live mini-map.
    max_seen = max((depth.get(p, 0) for p in all_paths if depth.get(p, 99) < 99), default=0)
    flow_nodes = []
    for ch in out_chapters:
        for s in ch["stops"]:
            p = s["path"]
            d = s["depth"] if s["depth"] < 99 else max_seen + 1
            flow_nodes.append({
                "id": p, "label": p.rsplit("/", 1)[-1], "role": _role(p),
                "dir": _dirname(p), "path": p, "depth": d, "entry": s["is_entry"],
                "chapter": ch["title"], "symbols": _symbols(texts.get(p, ""), p),
            })
    flow_edges = []
    seen_edge: set = set()
    for src in all_paths:
        for dst in out_adj.get(src, []):
            if dst in stop_set and dst != src and (src, dst) not in seen_edge:
                seen_edge.add((src, dst))
                flow_edges.append({"from": src, "to": dst, "kind": "import",
                                   "imports": _imported_names(texts.get(src, ""), dst)})
    flow_graph = {"nodes": flow_nodes, "edges": flow_edges,
                  "entry": entries[0] if entries else None}

    wiring = None
    try:
        wiring = build_wiring(ns, all_paths, settings=settings)
    except Exception:
        wiring = None

    return {
        "namespace": ns, "overview": overview,
        "entry_point": entries[0] if entries else None, "entry_points": entries,
        "total_stops": len(all_paths), "chapters": out_chapters, "wiring": wiring,
        "flow_graph": flow_graph,
        "narrated": any(s.get("insight") for ch in out_chapters for s in ch["stops"]),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cache (tour.json) — an agent run is slow, so keep the result
# ─────────────────────────────────────────────────────────────────────────────

def tour_cache_path(store, ns: str) -> Path:
    return store.ns_dir(ns) / "tour.json"


def load_cached_tour(namespace: str, *, settings: Optional[Settings] = None) -> Optional[dict]:
    store = get_store(settings or get_settings())
    p = tour_cache_path(store, slugify(namespace))
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_cached_tour(namespace: str, doc: dict, *, settings: Optional[Settings] = None) -> None:
    store = get_store(settings or get_settings())
    try:
        tour_cache_path(store, slugify(namespace)).write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
