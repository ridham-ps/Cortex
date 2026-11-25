"""The repo's import graph — which source file imports which (static, no LLM).

Used by the tour to connect the stops the agent chose ("this file uses X and Y",
the mini-map) and by the walkthrough to pick files for its wiring diagram.
"""
from __future__ import annotations

import os
from collections import Counter, deque
from typing import Optional

from cortex.graph.wiring import _IMPORT, _basekey, _dirname, _import_basekey, _role
from cortex.index.store import _NOISE_PATH

# source files that take part in the import graph
_SOURCE_EXT = {".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".java", ".rb", ".rs",
               ".cs", ".kt", ".scala", ".php", ".swift", ".c", ".cpp", ".vue"}
# breadth-first order: wiring modules first, data models later
_ROLE_RANK = {"module": 0, "component": 1, "binding": 1, "service": 2, "code": 2,
              "model": 3, "template": 4, "style": 4, "doc": 5}


def is_source_file(path: str) -> bool:
    """A real source file (not tests/specs/mocks) that can appear in the graph."""
    if _NOISE_PATH.search(path):
        return False
    return os.path.splitext(path.lower())[1] in _SOURCE_EXT


def text_by_path(docs: list[dict]) -> dict[str, str]:
    """Stitch every file back together from its indexed chunks."""
    out: dict[str, str] = {}
    for c in docs:
        p = c.get("metadata", {}).get("path", "")
        if p:
            out[p] = out.get(p, "") + "\n" + c.get("text", "")
    return out


def build_import_graph(paths: list[str], texts: dict[str, str]):
    """Directed import graph over the given source files.
    Returns (out_adj: file -> files it imports, indeg: file -> how many import it)."""
    basekey_to_paths: dict[str, list[str]] = {}
    for p in paths:
        basekey_to_paths.setdefault(_basekey(p), []).append(p)

    def resolve(target: str, src: str) -> Optional[str]:
        cands = basekey_to_paths.get(_import_basekey(target))
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        sdir = _dirname(src)
        # prefer same directory, then longest shared path prefix, then shortest path
        return sorted(cands, key=lambda p: (_dirname(p) != sdir,
                                            -_shared_prefix(p, src), len(p), p))[0]

    out_adj: dict[str, list[str]] = {p: [] for p in paths}
    indeg: Counter = Counter()
    for src in paths:
        for m in _IMPORT.findall(texts.get(src, "")):
            for raw in m:
                if not raw:
                    continue
                dst = resolve(raw, src)
                if dst and dst != src and dst not in out_adj[src]:
                    out_adj[src].append(dst)
                    indeg[dst] += 1
    return out_adj, indeg


def bfs_depth(entries: list[str], out_adj: dict[str, list[str]], indeg: Counter) -> dict[str, int]:
    """Import-graph distance from the entry point(s). Unreached files are absent."""
    max_visit = 250
    depth = {e: 0 for e in entries}
    q = deque(entries)
    while q and len(depth) < max_visit:
        cur = q.popleft()
        for dst in sorted(out_adj.get(cur, []),
                          key=lambda p: (_ROLE_RANK.get(_role(p), 2), -indeg.get(p, 0), p)):
            if dst not in depth:
                depth[dst] = depth[cur] + 1
                q.append(dst)
    return depth


def _shared_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a.split("/"), b.split("/")):
        if x != y:
            break
        n += 1
    return n
