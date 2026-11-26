# Evaluation Guide — Cortex

Cortex is checked in two separate ways, because they answer different questions:

| | Test suite | Live benchmarks |
|---|---|---|
| Question it answers | Do the rules around the agents work? | Are the real agent's answers good? |
| Calls Claude? | **Never** | Yes (your Claude login) |
| Speed | ~10 s | Minutes |
| Runs in CI? | Yes, every push | No, run by hand |
| Command | `python -m pytest -q` | `python -m evals.ragas_eval` · `python -m evals.repoqa_eval` |

---

## 1. The test suite (`tests/`)

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Every agent run in Cortex goes through one function: `run_tool_agent()` in
`cortex/agents/loop.py`. `tests/conftest.py` replaces it for every test:

- **By default, with a guard.** A test that reaches the agent loop without asking for a
  scripted agent fails immediately with "test tried to call Claude". The suite can never
  use your subscription by accident.
- **With the `fake_agent` fixture,** by a scripted agent. You set which files it opens
  (`fake_agent.reads`) and what it answers (`fake_agent.reply`). It opens the files through
  the **real** `ToolExecutor`, so `used_paths`, source grounding and path validation behave
  exactly as in production; only the model's decisions are scripted.

```python
def test_chat_sources_are_files_named_and_read(repo_ns, fake_agent):
    fake_agent.reads = ["etl/main.py", "etl/extract.py", "etl/load.py"]
    fake_agent.reply = "Execution starts in `etl/main.py` ... `etl/extract.py` ... `etl/unused.py`."
    r = ask(AskRequest(namespace=repo_ns, question="where does the pipeline start?"))
    # load.py was read but not named; unused.py was named but never read
    assert {s.path for s in r.sources} == {"etl/main.py", "etl/extract.py"}
```

| File | Covers |
|---|---|
| `test_agents.py` | The three agents: grounded chat sources, honest not-found, greetings skipping the agent, missing login reported (not hidden); tour and walkthrough built from the agent's structure with invented/unseen paths dropped; failures raised clearly |
| `test_builtin_tools.py` | The fence (paths outside the repo or in `.git` refused), recording what the agent saw, kept clones |
| `test_engine.py` | The engine under the agents: chunking, search ranking, ingest, re-sync, wiring, chat-history storage, annotations, path safety |
| `test_api.py` | HTTP layer: auth, rate limit, size limits, error codes, tour caching, tour failure → 503, chat history endpoints |
| `test_claude_connection.py` | Subscription login rules and the search_code tool |
| `test_install_guide.py` | Setup steps and prerequisites from config files |

**Adding a test for agent behaviour:** request `fake_agent`, script `reads` and `reply`
(a string, a dict that becomes JSON, or a function `(system_prompt, prompt) -> reply` when
one test drives several agents, like the walkthrough planner and writers), then assert on
what Python did with it.

---

## 2. Live benchmarks (`evals/`)

These run the **real** agent and need your Claude login. Run them after changing a prompt,
the tools or the model.

### RAGAS-style scorecard: `evals/ragas_eval.py`

Asks the questions in `evals/probe_questions.json` and has an LLM judge score each answer
(0.0–1.0):

- **faithfulness:** are the answer's claims supported by the code the agent read?
- **answer_relevancy:** does it answer the question?
- **context_relevance:** was what it read relevant?
- **abstention:** for a feature that doesn't exist, did it say so with no sources?

The probe targets the namespace named in `probe_questions.json` (`cortex`), so index this
repo under that name first:

```powershell
.\.venv\Scripts\python.exe -m cli.main ingest --repo . -n cortex
.\.venv\Scripts\python.exe -m evals.ragas_eval
```

| Variable | Default | Meaning |
|---|---|---|
| `AB_MODELS` | `haiku,sonnet,opus` | Models to compare |
| `AB_JUDGE_MODEL` | `sonnet` | Model that scores the answers |
| `AB_LIMIT` | `0` (all) | Only the first N questions (smoke test) |

Writes `evals/ragas_results.json`.

### Code search benchmark: `evals/repoqa_eval.py`

Adapted from the public RepoQA "find the function from its description" benchmark: it
indexes RepoQA repos, asks the agent to locate each described function, and checks whether
it names the right one. It's comparable to the RepoQA leaderboard only loosely, because
the protocol is different.

```powershell
.\.venv\Scripts\python.exe -m evals.repoqa_eval
```

| Variable | Default | Meaning |
|---|---|---|
| `AB_MODELS` | `haiku,sonnet,opus` | Models to compare |
| `REPOQA_LANGS` | `typescript,python` | Languages to sample |
| `REPOQA_REPOS` | `1` | Repos per language |
| `REPOQA_NEEDLES` | `5` | Functions to find per repo |

Writes `evals/repoqa_results.json`; the dataset is cached in `evals/.cache/`.

Both benchmarks take an output path override: `RAGAS_OUT=...` / `REPOQA_OUT=...`.

### Benchmark results

Run with the subscription's default model, judge `sonnet`. This compared Claude Code's
built-in tools against Cortex's former 9 hand-written tools (since removed):

| | Built-in tools (current) | 9 custom tools (removed) |
|---|---|---|
| RepoQA: right function **and** file (10 tasks, TypeScript + Python) | 10/10 | 10/10 |
| RAGAS faithfulness | 0.870 | 0.872 |
| RAGAS answer relevancy | 0.960 | 0.962 |
| RAGAS context relevance | 0.850 | 0.856 |
| Honest "not found" (negative control) | 1.0 | 1.0 |

The modes are equal within noise: per-question scores swing both ways (±0.25) on a
5-question set. Cortex now uses only the built-in tools, because Anthropic maintains them.

**How the judge sees sources.** Agents cite `file.py:42`, so the RAGAS judge is shown each
cited file **from disk with numbered lines**. Without numbers, the judge can't check
line references and marks correct answers as unsupported: an earlier run scored
`builtin` 0.80 faithfulness for exactly this reason.

**Rate limits.** A long benchmark can exhaust the subscription's quota; RepoQA then
reports `rate_limit (exhausted retries)` for those tasks. Wait for the limit to reset
and re-run, and increase `REPOQA_PACE_S` (seconds between questions).

---

## CI

`.github/workflows/ci.yml` installs the requirements and runs `python -m pytest -q` on every
push and pull request. A failing test blocks the build. The live benchmarks are not in CI,
because they need a Claude login and cost subscription usage.

## When something fails

| Symptom | Likely cause | Where to look |
|---|---|---|
| `AssertionError: test tried to call Claude` | A test runs an agent flow without `fake_agent` | Add the `fake_agent` fixture to the test |
| A chat source is missing or extra | Grounding rule changed | `_grounded_sources` in `cortex/agents/chat.py` |
| A tour stop or walkthrough file is dropped | Path not indexed, or the scripted agent never "read" it | `resolve_indexed_path` in `cortex/agents/loop.py`; the test's `reads` list |
| RAGAS faithfulness drops after a prompt change | The prompt lets the agent answer from memory | The system prompt at the top of `cortex/agents/chat.py` / `tour.py` / `walkthrough.py` |
| RAGAS says namespace not indexed | The probe's namespace isn't ingested | Ingest with `-n cortex` (see above) |
