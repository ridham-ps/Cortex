# Known Limitations & Future Hardening — Cortex

This document is honest about what Cortex is and isn't today. Read it before deploying to a shared environment.

---

## Current limitations

### Auth is basic
One shared API key grants full access to all namespaces. If you leave `ONBOARDING_API_KEYS` empty, the API is completely open.

**Impact**: Anyone with the key can read any indexed repo, ingest new repos, or clear chat history.  
**Mitigation now**: Set a strong key and rotate it. Keep the server off the public internet.  
**Future hardening**: Per-namespace ACLs, user-level tokens, OAuth integration.

---

### Single-node only
The rate limiter, vector index, and chat history all live in memory or on local disk. There is no shared state between multiple server instances.

**Impact**: Cannot horizontally scale; a restart clears the in-memory rate limiter.  
**Mitigation now**: Run one instance; use `ONBOARDING_CHAT_STORE=mongo` for durable chat history.  
**Future hardening**: Redis-backed rate limiter, shared object store for indexes, distributed index sharding.

---

### Large repos are sampled
The ingestor reads files up to sensible size and count limits. Very large monorepos are not fully indexed.

**Impact**: Questions about files beyond the ingest limit will return "not found."  
**Mitigation now**: Use `ONBOARDING_VECTOR_BACKEND=hybrid` for better signal on sampled content.  
**Future hardening**: Streaming ingestion, chunk pagination, user-configurable file-size caps.

---

### Ownership is approximate
"Who works on what" is inferred from git commit frequency per folder — not precise authorship or code review patterns.

**Impact**: Ownership attributions may be wrong for repos with infrequent committers or shared modules.  
**Future hardening**: Parse CODEOWNERS files, integrate with GitHub/GitLab blame APIs.

---

### Tests don't judge the model
The test suite replaces the agent with a scripted one, so it verifies grounding, path validation, not-found handling and the API contracts, but not whether the real agent's answers are good.

**Impact**: A prompt or model change can make answers worse without failing any test.  
**Mitigation**: Run the live benchmarks (`evals/ragas_eval.py`, `evals/repoqa_eval.py`) after prompt or model changes; see `docs/eval-guide.md`.

---

### Prompt injection is defended, not proven
Every system prompt declares repo content to be DATA, never instructions (the chat, tour and walkthrough agents' rules say so for all tool results). This is a prompt-level defence only — there is no automated injection test yet, because the test suite uses a scripted agent, not a real model.

**Impact**: A malicious README with carefully crafted instructions might partially influence output.  
**Mitigation now**: Agents are read-only, so an injection can at worst distort an answer — it cannot write files or run commands.  
**Future hardening**: Add injection cases to the live `ragas_eval` run; red-team with a wider range of patterns; consider output filtering.

---

### Agentic tour/walkthrough cost more time and quota
The tour and walkthrough are designed by agents, not filled from a fixed template. On a
~50-file repo a fresh tour took ~50 s (one agent run, up to 30 tool turns) and a walkthrough
~3 min (one planner run + one writer run per section, 4 in parallel). Both are cached
(`tour.json`, `walkthrough.json`), so this is paid once per repo until a refresh. The
`/v1/tour` request is synchronous, so the first uncached call blocks for that long.
Parallel writers each spawn a Claude Code CLI; their starts are staggered because
simultaneous starts can race on the CLI's `~/.claude.json`.

### No streaming for tour/walkthrough
Long-running generation endpoints (`/v1/tour`, `/v1/walkthrough`) are polled — the client re-requests until the result is ready. Only `/v1/ask/stream` emits SSE events.

**Future hardening**: Add SSE / WebSocket streaming for all generation endpoints.

---

### No write or execution tools
Agents are strictly read-only. They cannot modify files, run commands, apply patches, or create pull requests.

**This is intentional** — read-only is the safest scope for an onboarding tool.  
**Future hardening** (if needed): Gated write tools with user confirmation flow.

---

## Deployment checklist

Before putting Cortex on a shared or networked server:

- [ ] Set `ONBOARDING_API_KEYS` to one or more strong, unique keys (not `dev-local-key`)
- [ ] Set `ONBOARDING_ALLOWED_ROOTS` to restrict which directories can be ingested
- [ ] Set `ONBOARDING_RATE_LIMIT_PER_MIN` appropriate for your expected load
- [ ] Use `ONBOARDING_CHAT_STORE=mongo` with a secured MongoDB for durable history
- [ ] Put the server behind a reverse proxy (nginx/Caddy) with TLS
- [ ] Review `ONBOARDING_LOG_LEVEL` — default INFO is fine; DEBUG logs chunk content
- [ ] Run `python -m pytest -q` (and the live benchmarks after prompt/model changes) before deploying

---

## Roadmap ideas

| Area | Idea |
|---|---|
| Auth | Per-namespace ACLs, OAuth/SSO |
| Scale | Redis rate limiter, S3-backed index |
| Evals | LLM-as-judge pipeline on real repos, eval dashboard |
| Agents | Cross-repo Q&A (multiple namespaces in one answer) |
| Tooling | PR diff agent ("what changed and why?") |
| Tooling | Annotation suggestions ("this function needs a doc comment") |
| UI | Mobile-friendly layout; PDF export for the guided tour (walkthrough PDF already ships) |
