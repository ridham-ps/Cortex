# Architecture — Cortex

Cortex helps engineers learn an unfamiliar codebase. It indexes a repository, then
**Claude agents** answer questions, design a guided tour and write a project walkthrough.
Every agent decides for itself what to read, using Claude Code's own read-only tools
(Read, Grep, Glob) plus one Cortex search tool, and everything it says is checked against
the code it actually read.

## Code map

```
cortex/
  agents/                 ★ the AI side: every agent and the loop they share
    loop.py                 run_tool_agent(): the ONE place Claude runs as an agent
    claude.py               how Cortex connects to Claude (subscription login, options)
    tools.py                search_code (Cortex's one custom tool) + the per-run record
    chat.py                 chat agent: answers one question with cited sources
    tour.py                 tour agent: designs a guided reading tour
    walkthrough.py          planner + writer agents: a long-form walkthrough
  index/                  how a repo becomes searchable (no LLM)
    ingest.py               clone/read → chunk → enrich → write the index
    chunker.py  enrich.py   splitting files; adding i18n labels + git commits
    store.py                the search index (TF-IDF) + get_store()
    dense_store.py  hybrid_store.py   optional embedding / combined search
    access.py               ONBOARDING_ALLOWED_ROOTS folder fence
  graph/                  static code structure (no LLM)
    imports.py              which file imports which (connects tour stops)
    wiring.py               the "how these files connect" diagram
  storage/                what Cortex remembers per repo
    chat_history.py         past questions + answers (JSON or MongoDB)
    annotations.py          team notes about files (shown on tour stops)
  install_guide.py        the only non-AI helper: setup steps from config files
  walkthrough_pdf.py      walkthrough → PDF
  config.py  contract.py  trace.py   settings, request/response shapes, logging
api/server.py             FastAPI: REST API + serves the web UI
cli/main.py               command line (ingest, ask, namespaces, info)
web/index.html            the single-page web UI
tests/                    offline tests (a scripted agent stands in for Claude)
evals/                    live benchmarks of the real agent (need a Claude login)
```

**Suggested reading order:** `agents/loop.py` → `agents/tools.py` → `agents/chat.py` →
`agents/tour.py` → `agents/walkthrough.py`. Then `index/ingest.py` to see how the data
the tools read is produced.

## How a request flows

```
Web UI / CLI / REST
      │
api/server.py ── auth · rate limit · size limit
      │
      ├─ POST /v1/ingest ─────▶ index/ingest.py ─▶ <index>/<repo>/  (chunks, search index)
      │
      ├─ POST /v1/ask ────────▶ agents/chat.py ─┐
      ├─ GET  /v1/tour ───────▶ agents/tour.py ─┼─▶ agents/loop.py ─▶ Claude
      └─ POST /v1/walkthrough ▶ agents/walkthrough.py ┘      ▲  │
                                                           │  ▼
                     Read / Grep / Glob ─▶ repo files on disk (fenced to the repo folder)
                     search_code ───────▶ index/store.py
```

## The agent loop (`agents/loop.py`)

`run_tool_agent()` is the only function that runs Claude as an agent. Claude owns the
control flow; Python only executes what Claude asks for:

```
Python                                      Claude (Claude Agent SDK → Claude Code CLI)
──────                                      ───────────────────────────────────────────
query(prompt, options) ───────────────────▶ reads the question
                                            picks a tool ──┐
tool runs  ◀──────────── "call Grep(...)" ──────────────────┘
  files it showed → executor.used_paths
result text ─────────────────────────────▶ reads it, picks the next tool, …
◀──────────────────────────── final answer ─ decides it has enough (or hits max turns)
```

| | |
|---|---|
| Tools | Claude Code's own **Read / Grep / Glob**, working directory = the repo folder, plus Cortex's **`search_code`** (`agents/tools.py`, served as an in-process MCP server: `mcp__kt__search_code`) |
| System prompt | Claude Code's own prompt (`preset: claude_code`) with the agent's role and rules, and the tool guide (`TOOLS_GUIDE`), appended |
| Fence | A `PreToolUse` hook denies any path outside the repo folder or inside `.git` |
| What "the agent read" means | A `PostToolUse` hook records files Read, plus indexed files listed by Grep/Glob; `search_code` records the files it returns |
| Locked down | No other tools, no shell, nothing can be modified; `setting_sources=None` ignores any CLAUDE.md or settings files, including ones inside the indexed repo |

The SDK is async; `run_sync()` (in `agents/claude.py`) runs it on its own thread so the
rest of the app can stay synchronous.

**Why the built-in tools:** Anthropic maintains them and trains Claude on them. They
were benchmarked against Cortex's former 9 hand-written tools and scored the same
(see [eval-guide](eval-guide.md#benchmark-results)), so the custom tools were removed.

## The agents

| Agent | File | Turns | What it returns | How Python checks it |
|---|---|---|---|---|
| Chat (`kt-agent-v1`) | `agents/chat.py` | 12 (run retried up to 2×) | Structured output (`CHAT_SCHEMA`): `found`, Markdown `answer`, `sources` with line ranges | Sources = files Claude cited, kept only if indexed **and** read; `found: false` gives no sources and `grounded: false` |
| Tour | `agents/tour.py` | 30 | Structured output (`TOUR_SCHEMA`): overview, entry point, chapters of stops | Stops kept only if indexed **and** seen by the agent; connections come from the import graph, not the model |
| Walkthrough planner | `agents/walkthrough.py` | 30 | Structured output (`PLANNER_SCHEMA`): stack, 5–10 sections with files | Invented file paths dropped; fewer than 3 sections means failure |
| Walkthrough writers | `agents/walkthrough.py` | 14 each, 4 in parallel | Structured output (`WRITER_SCHEMA`): explanation, takeaways, files | Section `files` = files the writer cited, kept only if real **and** read |
| Install guide | `install_guide.py` | — (no LLM) | Setup steps + prerequisites | Fixed rules over config files |

Each agent's system prompt and JSON schema are at the top of its own file. The schemas use the SDK's structured output (`output_format`): the SDK
guarantees the shape, and Python only checks what a schema can't, namely that every path
is real and was actually read. There is no fallback to a
non-agent path: if an agent can't run or returns nothing usable, the API returns a 503
with the reason (`AgentUnavailableError` or `AgentFailedError` in `agents/loop.py`).

## The tools

| Tool | From | What it does |
|---|---|---|
| `Read` | Claude Code | Read a file (with line numbers) |
| `Grep` | Claude Code | ripgrep: exact strings / regex across the repo |
| `Glob` | Claude Code | Find files by name pattern |
| `search_code` | Cortex (`agents/tools.py`) | Ranked search over the index: code, recent git commits, UI text labels |

`ToolExecutor` (in `agents/tools.py`) is created per agent run and records every call
(`call_log`) and every file the agent saw (`used_paths`). Grounding is checked
against `used_paths`.

## Ingest (`index/ingest.py`)

```
POST /v1/ingest (folder path or git URL)
  → namespace = slug of the repo name; already indexed and no rebuild? serve the cache
  → git URL: shallow clone into a temp folder (token sent as a header, never stored)
  → ONBOARDING_ALLOWED_ROOTS check (index/access.py)
  → chunker: skip heavy/binary folders; ≤ 4000 files, ≤ 60 KB each; split at
    function/class/heading boundaries; label each chunk with path, lines, symbol
  → enrich: i18n labels folded in; last 100 git commits added as their own chunks
  → a clone is KEPT: moved to <repos>/<namespace> so agents can read the files
    (a local folder is read in place, never copied)
  → store.index() writes <index>/<namespace>/, with repo_path in meta.json
```

`index.ingest.repo_folder(namespace)` returns that folder; re-sync re-clones and replaces it.
For running this on a server, see [deployment.md](deployment.md).

## Storage layout

```
.kt_index/                    (ONBOARDING_INDEX_DIR)
  <namespace>/
    chunks.json               indexed text chunks with metadata
    meta.json                 repo path, clone URL, file count, backend
    tfidf.joblib              TF-IDF search index
    dense.npy, dense_keys.json  embeddings (dense/hybrid backends only)
    tour.json                 cached tour (agent-built)
    walkthrough.json          cached walkthrough (agent-built)
    chat_history.json         conversation history (or MongoDB)
    annotations.json          team notes

.kt_repos/                    (ONBOARDING_REPOS_DIR)
  <namespace>/                kept shallow clone of a git-URL repo

trace.json                    one JSON line per request (ONBOARDING_TRACE_FILE)
```

## Connecting to Claude (`agents/claude.py`)

Cortex uses the **Claude Agent SDK** on a Claude Pro/Max **subscription over OAuth**, with
no billed API key. `resolve_subscription_auth()` enforces the rules:

- `ANTHROPIC_API_KEY` set → always refuse (it would override the subscription and bill the API).
- Otherwise it needs `CLAUDE_CODE_OAUTH_TOKEN` or a login in `~/.claude/.credentials.json`.

The model is `CLAUDE_SDK_MODEL`; blank means the subscription's default.

## Security model

- **Read-only tools:** agents can read indexed content only; no write, execute or shell access.
- **Filesystem fence:** `ONBOARDING_ALLOWED_ROOTS` restricts which folders can be indexed.
- **Prompt injection defence:** every agent's system prompt states that tool results are
  repository DATA, never instructions.
- **Grounding validation:** sources and tour/walkthrough paths must be indexed files the agent
  actually read, so an invented path can never appear.
- **Bearer token auth:** constant-time comparison prevents timing attacks.
- **Secrets never logged:** API keys and clone tokens are used in memory only.

## Observability

Every request produces:

- A `trace_id` (`tr_<16 hex>`) on every log line and in error responses.
- Structured JSON log lines on stderr (timestamp, level, event, trace_id).
- A JSON line in `trace.json`: namespace, question excerpt, tool calls, files accessed,
  turns, duration.
- A `trace` object in each chat response, with the full tool call log (`grounding.call_log`).
