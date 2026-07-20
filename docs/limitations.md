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

### Quality gate checks structure, not prose
The 31-case eval suite verifies grounding, retrieval relevance, hallucination prevention, and injection resistance. It does NOT score whether an answer is well-written, complete, or useful.

**Impact**: The gate can pass while answer quality degrades silently if only prose changes.  
**Future hardening**: Add LLM-as-judge layer for narrative quality; see `docs/eval-guide.md`.

---

### Prompt injection is defended, not proven
The DATA/instruction boundary in system prompts (`<<<REPO_CONTEXT_JSON>>>` markers) is tested against one representative injection fixture. Sophisticated adversarial inputs may still find gaps.

**Impact**: A malicious README with carefully crafted instructions might partially influence output.  
**Mitigation now**: The eval test `injection_in_readme_neutralized` catches the obvious case.  
**Future hardening**: Red-team with a wider range of injection patterns; consider output filtering.

---

### No streaming for tour/walkthrough
Long-running generation endpoints (`/v1/tour`, `/v1/walkthrough`) are polled — the client re-requests until the result is ready. Only `/v1/ask/stream` emits SSE events.

**Future hardening**: Add SSE / WebSocket streaming for all generation endpoints.

---

### No write or execution tools
Agents are strictly read-only. They cannot modify files, run commands, apply patches, or create pull requests.

**This is intentional** — read-only is the safest scope for an onboarding tool.  
**Future hardening** (if needed): Gated write tools with user confirmation flow.

---

### Model-fallback budget is approximate
The subscription's real usage quota is **not** exposed to the app. The proactive budget
(`ONBOARDING_SOFT_BUDGET_PER_WINDOW`) is therefore a **local, per-process** rolling-window count
of requests — not the account-wide quota, not token-accurate, and not shared across instances.

**Impact**: On a restart the counter resets; with multiple instances each counts only its own
traffic. It can downgrade too early or too late relative to your actual quota.  
**Mitigation now**: The **reactive ladder** (`CLAUDE_SDK_FALLBACKS`) is the accurate safety net —
it fires on the real limit error regardless of the estimate.  
**Future hardening**: Token-based accounting; a shared (Redis) counter for multi-node.

---

### Cross-repo debug has no combined wiring diagram yet
Cross-repo debug answers show per-repo grouped, colour-tagged sources and a "repos involved"
row, but not a single front→back graph spanning the repos (single-repo answers still get their
wiring diagram).

**Future hardening**: Build a combined cross-repo flow graph (match UI calls to backend routes).

---

## Deployment checklist

Before putting Cortex on a shared or networked server:

- [ ] Set `ONBOARDING_API_KEYS` to one or more strong, unique keys (not `dev-local-key`)
- [ ] Set `ONBOARDING_ALLOWED_ROOTS` to restrict which directories can be ingested
- [ ] Set `ONBOARDING_RATE_LIMIT_PER_MIN` appropriate for your expected load
- [ ] Use `ONBOARDING_CHAT_STORE=mongo` with a secured MongoDB for durable history
- [ ] Put the server behind a reverse proxy (nginx/Caddy) with TLS
- [ ] Review `ONBOARDING_LOG_LEVEL` — default INFO is fine; DEBUG logs chunk content
- [ ] Run `python -m evals.runner` and confirm `gate_passed: true` before deploying

---

## Roadmap ideas

| Area | Idea |
|---|---|
| Auth | Per-namespace ACLs, OAuth/SSO |
| Scale | Redis rate limiter, S3-backed index |
| Evals | LLM-as-judge pipeline on real repos, eval dashboard |
| Agents | ✅ Cross-repo debugging (projects) — shipped; next: combined cross-repo wiring graph |
| Tooling | PR diff agent ("what changed and why?") |
| Tooling | Annotation suggestions ("this function needs a doc comment") |
| UI | Mobile-friendly layout, PDF export for walkthrough |
