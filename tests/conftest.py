"""Test setup. Tests never call Claude.

Every agent run goes through ONE function, `loop.run_tool_agent` (cortex/agents/loop.py). Here it is
replaced for every test:
  * by default with a guard that fails the test if anything tries to reach Claude;
  * by the `fake_agent` fixture with a scripted agent, for tests that exercise an
    agent flow (chat, tour, walkthrough).

The scripted agent "reads" files by recording them on the REAL ToolExecutor
exactly as the built-in Read hook does (`call_log` + `used_paths`), so every
grounding / validation rule behaves as in production — only the model's
decisions are scripted.
"""
import json
import os
import tempfile

import jsonschema
import pytest

os.environ["ONBOARDING_VECTOR_BACKEND"] = "tfidf"  # no model download/embedding in tests
# hermetic chat store: never touch a real MongoDB even if the dev .env sets a URI
os.environ["ONBOARDING_CHAT_STORE"] = "json"
os.environ["ONBOARDING_MONGO_URI"] = ""
os.environ["ONBOARDING_TRACE_FILE"] = os.path.join(tempfile.gettempdir(), "onboarding_test_trace.jsonl")
os.environ["ONBOARDING_ALLOWED_ROOTS"] = ""  # allow temp dirs
# Isolate the index and kept clones so tests never touch .kt_index / .kt_repos.
_TMP = tempfile.mkdtemp(prefix="kt_idx_")
os.environ["ONBOARDING_INDEX_DIR"] = os.path.join(_TMP, "index")
os.environ["ONBOARDING_REPOS_DIR"] = os.path.join(_TMP, "repos")


@pytest.fixture(autouse=True)
def _no_live_claude(monkeypatch):
    """Fail loudly if a test reaches the real agent loop without `fake_agent`."""
    import cortex.agents.loop as loop

    def _refuse(*_a, **_k):
        raise AssertionError("test tried to call Claude — request the fake_agent fixture")

    monkeypatch.setattr(loop, "run_tool_agent", _refuse)


class ScriptedAgent:
    """Stands in for Claude in `run_tool_agent`.

    reads: repo-relative paths the agent Reads (a list, or fn(system_prompt) -> list)
    reply: the final answer (str or dict -> JSON), or fn(system_prompt, prompt)
    """

    def __init__(self):
        self.reads = []
        self.reply = ""
        self.system_prompts: list[str] = []

    def __call__(self, executor, system_prompt, prompt, *, max_turns, settings=None,
                 event_callback=None, errors=None, output_schema=None):
        self.system_prompts.append(system_prompt)
        reads = self.reads(system_prompt) if callable(self.reads) else self.reads
        for path in reads:  # what loop._fence_hooks records for a built-in Read
            executor.call_log.append({"tool": "Read", "input": {"file_path": path}})
            executor.used_paths.add(path)
        reply = self.reply(system_prompt, prompt) if callable(self.reply) else self.reply
        turns = max(1, len(reads))
        if output_schema is None:  # plain text answer (chat)
            return (reply if isinstance(reply, str) else json.dumps(reply)), turns
        # structured output, like the SDK: a dict matching the schema, or {} when
        # the agent produced none. A scripted dict must match the schema — the real
        # SDK never returns a wrong shape, so a test must not either.
        if not isinstance(reply, dict):
            return {}, turns
        jsonschema.validate(reply, output_schema)
        return reply, turns


@pytest.fixture
def fake_agent(monkeypatch):
    """Script the agent: set `.reads` and `.reply`, then call the real code."""
    import cortex.agents.loop as loop
    import cortex.agents.walkthrough as walkthrough

    agent = ScriptedAgent()
    monkeypatch.setattr(loop, "run_tool_agent", agent)
    monkeypatch.setattr(walkthrough, "_WRITER_STAGGER_S", 0.0)
    return agent
