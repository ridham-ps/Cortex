"""The walkthrough agents — a long-form, plain-English deep dive of a repo.

The guided tour says "read these files, in this order." The walkthrough explains
the WHOLE project end-to-end, the way a senior engineer would onboard a new
teammate. It uses the orchestrator-workers pattern:

    PLANNER agent (`_plan`)                       one run, up to 30 turns
      explores the repo, designs an outline that fits THIS project:
      its own sections, titles and key files
            │
            ▼
    WRITER agents (`_write_section`)              one per section, 4 in parallel
      each investigates further and writes Markdown + 2-3 takeaways;
      a section's `files` = files its writer both opened AND named

Runs on a background thread (a few minutes) and is cached to
<index>/<repo>/walkthrough.json. If it fails, the error is kept for the API.
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from cortex.agents import loop
from cortex.agents.tools import ToolExecutor
from cortex.config import Settings, get_settings
from cortex.graph.imports import is_source_file
from cortex.graph.wiring import build_wiring
from cortex.index.store import get_store, slugify
from cortex.trace import logger

PLANNER_MAX_TURNS = 30
WRITER_MAX_TURNS = 14
_WRITER_WORKERS = 4
# Each agent run starts a Claude Code CLI process that rewrites ~/.claude.json
# on startup; simultaneous starts can race on that file, so the first wave of
# writers starts a little apart (later ones start as slots free up).
_WRITER_STAGGER_S = 2.0

PLANNER_SYSTEM_PROMPT = f"""\
You are a senior engineer PLANNING a long-form onboarding walkthrough of a
codebase for a teammate who has never seen it. You have read-only tools over the
indexed repo. Investigate first; then design an outline that fits THIS project.

HOW TO WORK:
1. Orient: the file layout, the dependency manifest(s), the README, and the entry point.
2. Identify how the project is really organised: its layers, feature areas, core
   domain logic, data, integrations, and anything unusual or especially important.
3. Design 5-10 sections in the order a newcomer should learn them. Sections must
   reflect this specific project — e.g. a compiler might need "Lexing", "Parsing",
   "Code generation"; a SPA might need "State management" and "Feature modules".
   Do not force sections that don't apply (no "Data models" if there is no data
   layer). Always start with an overview of what the project is, and end with how
   to build and run it if the repo shows that.
4. For each section, list the 2-8 files that best support it — only files you
   saw in a tool result.

{loop.AGENT_SAFETY}
When done investigating, return the outline.
"""

# Shapes of the planner's and writers' answers. The SDK makes each final answer
# match its schema (structured output); the descriptions guide the agent.
PLANNER_SCHEMA = {
    "type": "object",
    "required": ["stack", "sections"],
    "properties": {
        "stack": {"type": "array", "items": {"type": "string"},
                  "description": "the project's main languages and frameworks"},
        "sections": {"type": "array", "items": {
            "type": "object",
            "required": ["key", "title", "goal", "files"],
            "properties": {
                "key": {"type": "string", "description": "short kebab-case id"},
                "title": {"type": "string", "description": "section title in this project's terms"},
                "goal": {"type": "string", "description": "what this section must explain, 1-2 sentences"},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "2-8 repo-relative paths of files you saw"},
            }}},
    },
}

WRITER_SCHEMA = {
    "type": "object",
    "required": ["explanation", "takeaways"],
    "properties": {
        "explanation": {"type": "string", "description": "the section, in Markdown"},
        "takeaways": {"type": "array", "items": {"type": "string"},
                      "description": "2-3 key points, under 15 words each"},
    },
}

WRITER_SYSTEM_PROMPT = f"""\
You are a senior engineer writing ONE section of an onboarding walkthrough for a
brand-new teammate, in clear, plain English — like you're sitting next to them.
You have read-only tools over the indexed repo. Read the suggested files, and
follow imports/calls with the tools wherever needed to explain the section
properly. Do not stop at the suggested files if the real story is elsewhere.

WRITING RULES:
1. Ground EVERY statement in code you actually read with the tools. If the repo
   doesn't show something, don't claim it.
2. Mention the real file paths you describe inline in backticks
   (e.g. `src/auth/login.ts`) so the reader can open them.
3. Explain PURPOSE and FLOW — what this part does, why it exists, how its pieces
   connect to each other and to the rest of the app. No line-by-line narration.
4. Short paragraphs, "- " bullets for lists, **bold** for key names. No headings
   (the section title is added for you). No fluff.
5. `takeaways`: 2-3 short strings (under 15 words each) — the most important
   things a newcomer must remember from this section.

{loop.AGENT_SAFETY}
When done, return the section.
"""


def _planner_prompt(namespace: str, n_files: int) -> str:
    return (
        f"Repository: {namespace} ({n_files} indexed files).\n"
        f"Investigate the repo with the tools (including its languages, frameworks and "
        f"build tooling), then return the outline."
    )


def _writer_prompt(namespace: str, stack: list[str], outline: list[dict], section: dict) -> str:
    toc = "\n".join(f"{i + 1}. {s.get('title', '')}" for i, s in enumerate(outline))
    files = "\n".join(f"- {p}" for p in section.get("files") or []) or "- (none suggested — find them)"
    return (
        f"Repository: {namespace}. Stack: {', '.join(stack) or 'unknown'}.\n\n"
        f"Full walkthrough outline (for context — other sections are written separately, "
        f"so don't repeat their content):\n{toc}\n\n"
        f"Write the section: \"{section.get('title', '')}\"\n"
        f"Goal: {section.get('goal', '')}\n"
        f"Suggested starting files:\n{files}\n\n"
        f"Investigate with the tools, then return the section."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def build_walkthrough(namespace: str, *, settings: Optional[Settings] = None) -> dict:
    """Plan the outline with one agent, then write every section with writer agents.

    Raises ValueError if the namespace isn't indexed, loop.AgentUnavailableError
    if Claude can't be reached, and loop.AgentFailedError if the plan is unusable.
    """
    settings = settings or get_settings()
    store = get_store(settings)
    ns = slugify(namespace)
    if not store.exists(ns):
        raise ValueError(f"namespace not indexed: {ns}. Ingest the repo first.")
    known = store.known_paths(ns) - loop.PSEUDO_PATHS
    if not known:
        raise ValueError(f"no indexed content for namespace: {ns}")

    stack, outline = _plan(ns, store, known, settings)
    jobs = [(i, section, stack, outline) for i, section in enumerate(outline)]
    with ThreadPoolExecutor(max_workers=_WRITER_WORKERS) as pool:
        sections = list(pool.map(lambda job: _write_section(ns, store, known, settings, *job), jobs))

    # wiring diagram over the source files the walkthrough actually covers
    covered = list(dict.fromkeys(p for s in sections for p in s["files"] if is_source_file(p)))
    wiring = None
    try:
        wiring = build_wiring(ns, covered[:12] or sorted(p for p in known if is_source_file(p)),
                              settings=settings)
    except Exception:
        wiring = None

    return {
        "namespace": ns, "title": f"Project walkthrough — {ns}",
        "stack": stack, "sections": sections, "wiring": wiring,
        "generated_with": f"{settings.model_used} (agent)",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: the planner agent
# ─────────────────────────────────────────────────────────────────────────────

def _plan(ns: str, store, known: set[str], settings: Settings) -> tuple[list[str], list[dict]]:
    """Run the planner agent. Returns (stack, outline)."""
    planner = ToolExecutor(namespace=ns, store=store)
    errors: list[str] = []
    plan: dict = {}
    for _attempt in range(2):  # one retry on a transient failure
        try:
            plan, _ = loop.run_tool_agent(
                planner, PLANNER_SYSTEM_PROMPT, _planner_prompt(ns, len(known)),
                max_turns=PLANNER_MAX_TURNS, settings=settings, errors=errors,
                output_schema=PLANNER_SCHEMA)
            break
        except loop.AgentUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc)[:200])

    outline = _validate_outline(plan, known)
    if not outline:
        logger.warning("walkthrough_planner_failed namespace=%s errors=%s", ns, errors[-2:])
        raise loop.AgentFailedError(
            "the planner agent did not return a usable outline"
            + (f" ({errors[-1]})" if errors else "") + " — try again")
    stack = [s.strip()[:40] for s in plan["stack"] if s.strip()][:6]
    logger.info("walkthrough_plan namespace=%s sections=%d tool_calls=%d",
                ns, len(outline), len(planner.call_log))
    return stack, outline


def _validate_outline(obj: dict, known: set[str]) -> Optional[list[dict]]:
    """Clean the planner's outline: unique keys, non-empty titles, and only files
    that exist in the index (invented paths dropped). None if fewer than 3
    sections. The SDK already guarantees the shape (PLANNER_SCHEMA); {} means no answer."""
    out: list[dict] = []
    keys: set[str] = set()
    for s in obj.get("sections", [])[:12]:
        title = s["title"].strip()[:90]
        if not title:
            continue
        key = slugify(s["key"] or title)[:40] or f"section-{len(out) + 1}"
        while key in keys:
            key += "-2"
        keys.add(key)
        resolved = [p for p in dict.fromkeys(loop.resolve_indexed_path(f, known) for f in s["files"]) if p]
        out.append({"key": key, "title": title, "goal": s["goal"][:400], "files": resolved[:8]})
    return out if len(out) >= 3 else None


# ─────────────────────────────────────────────────────────────────────────────
# Step 2: the writer agents
# ─────────────────────────────────────────────────────────────────────────────

def _write_section(ns: str, store, known: set[str], settings: Settings,
                   idx: int, section: dict, stack: list[str], outline: list[dict]) -> dict:
    """One writer agent writes one section (runs on a worker thread)."""
    if idx < _WRITER_WORKERS:
        time.sleep(idx * _WRITER_STAGGER_S)
    ex = ToolExecutor(namespace=ns, store=store)
    body, takeaways = "", []
    try:
        reply, _ = loop.run_tool_agent(
            ex, WRITER_SYSTEM_PROMPT, _writer_prompt(ns, stack, outline, section),
            max_turns=WRITER_MAX_TURNS, settings=settings, errors=[], output_schema=WRITER_SCHEMA)
        body = reply.get("explanation", "").strip()
        takeaways = [t.strip() for t in reply.get("takeaways", []) if t.strip()][:3]
    except Exception as exc:  # noqa: BLE001 — one failed section must not sink the rest
        logger.warning("walkthrough_writer_failed key=%s error=%s", section["key"], str(exc)[:120])

    if not body:
        body = "_The writer agent could not complete this section. Regenerate the walkthrough to retry._"
    # grounded sources: files the writer both opened and named in its text
    cited = sorted(loop.cited_paths(body, ex.used_paths & known))
    files = cited or [f for f in section["files"] if f in ex.used_paths] or section["files"]
    return {"key": section["key"], "title": section["title"], "body": body,
            "takeaways": takeaways, "files": files[:8]}


# ─────────────────────────────────────────────────────────────────────────────
# Background job + cache (walkthrough.json)
# ─────────────────────────────────────────────────────────────────────────────
_WALK_JOBS: dict[str, threading.Thread] = {}
_WALK_ERRORS: dict[str, str] = {}
_WALK_JOBS_LOCK = threading.Lock()


def walkthrough_path(store, ns: str) -> Path:
    return store.ns_dir(ns) / "walkthrough.json"


def load_cached_walkthrough(namespace: str, *, settings: Optional[Settings] = None) -> Optional[dict]:
    store = get_store(settings or get_settings())
    p = walkthrough_path(store, slugify(namespace))
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def walkthrough_running(namespace: str) -> bool:
    with _WALK_JOBS_LOCK:
        t = _WALK_JOBS.get(slugify(namespace))
        return bool(t and t.is_alive())


def walkthrough_error(namespace: str) -> Optional[str]:
    """Why the last background run failed, if it did (cleared on the next start)."""
    with _WALK_JOBS_LOCK:
        return _WALK_ERRORS.get(slugify(namespace))


def fire_walkthrough_background(namespace: str, *, settings: Optional[Settings] = None) -> bool:
    """Start generating the walkthrough on a daemon thread. Returns False if one
    is already running for this namespace."""
    settings = settings or get_settings()
    ns = slugify(namespace)
    store = get_store(settings)
    with _WALK_JOBS_LOCK:
        existing = _WALK_JOBS.get(ns)
        if existing and existing.is_alive():
            return False
        _WALK_ERRORS.pop(ns, None)

    def _run() -> None:
        try:
            doc = build_walkthrough(ns, settings=settings)
            walkthrough_path(store, ns).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            logger.info("walkthrough_done namespace=%s sections=%d", ns, len(doc.get("sections", [])))
        except Exception as exc:  # noqa: BLE001
            logger.exception("walkthrough_failed namespace=%s", ns)
            with _WALK_JOBS_LOCK:
                _WALK_ERRORS[ns] = str(exc)[:300] or type(exc).__name__
        finally:
            with _WALK_JOBS_LOCK:
                _WALK_JOBS.pop(ns, None)

    t = threading.Thread(target=_run, daemon=True, name=f"walkthrough-{ns}")
    with _WALK_JOBS_LOCK:
        _WALK_JOBS[ns] = t
    t.start()
    return True
