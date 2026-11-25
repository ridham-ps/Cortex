"""Cortex's one custom tool, and the per-run record of what an agent saw.

The agents read code with Claude Code's own built-in tools — Read, Grep and Glob
(see `loop.py`). Cortex adds just one tool those can't replace:

  search_code — ranked search over the repo's index: code, recent git commits
                and UI text labels. Good for "where is the login flow?" when you
                don't know any exact name to grep for.

`ToolExecutor` is created once per agent run. It runs `search_code` and records
every tool call (`call_log`) and every file the agent was shown (`used_paths`).
Grounding is checked against `used_paths`: a file can only become a source if
the agent actually saw it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cortex.index.store import VectorStore

# The schema Claude sees for search_code (exposed as mcp__kt__search_code).
SEARCH_CODE_TOOL: dict = {
    "name": "search_code",
    "description": (
        "Ranked search over the indexed repository — its code, its recent git commits "
        "and its UI text labels. Returns matching snippets with file paths and line "
        "numbers. Use it when you know a concept or feature but not an exact name; "
        "use Grep for exact strings."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "What to search for: a concept, feature or keyword."},
            "top_k": {"type": "integer", "description": "Number of results (default 8, max 20)."},
        },
        "required": ["query"],
    },
}


class ToolExecutor:
    """One agent run's tool record: runs search_code, logs every call, and
    tracks which files the agent saw (the basis of every grounding check)."""

    def __init__(self, namespace: str, store: "VectorStore") -> None:
        self.namespace = namespace
        self.store = store
        self.used_paths: set[str] = set()
        self.call_log: list[dict[str, Any]] = []

    def search_code(self, query: str, top_k: int = 8) -> str:
        self.call_log.append({"tool": "search_code", "input": {"query": query, "top_k": top_k}})
        try:
            k = max(1, min(int(top_k), 20))
            results = self.store.search(self.namespace, query, k)
        except Exception as exc:  # noqa: BLE001
            return f"Tool error (search_code): {exc}"
        if not results:
            return f"No results for '{query}'. Try other terms, or Grep for an exact name."
        parts = []
        for r in results:
            m = r.get("metadata", {})
            path = m.get("path", "unknown")
            self.used_paths.add(path)
            parts.append(f"[{path}:{m.get('line_start', 0)}-{m.get('line_end', 0)}] "
                         f"(relevance {r.get('score', 0):.3f})\n{r.get('text', '').strip()}")
        return "\n\n---\n\n".join(parts)
