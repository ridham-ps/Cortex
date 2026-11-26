"""The Claude connection (cortex/agents/claude.py) and the loop's tool wiring
(cortex/agents/loop.py). No live Claude calls: login rules, config plumbing, and
Cortex's one custom tool, search_code.
"""
import asyncio
import dataclasses

import pytest

from cortex.agents.claude import get_claude, resolve_subscription_auth
from cortex.config import get_settings


def _sdk_settings():
    return get_settings()


# ── config plumbing ──────────────────────────────────────────────────────────

def test_model_used_reports_claude_sdk():
    s = _sdk_settings()
    assert s.model_used.startswith("claude_sdk/")


def test_get_claude_refuses_a_billed_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    with pytest.raises(RuntimeError):
        get_claude(_sdk_settings())


# ── auth resolution ──────────────────────────────────────────────────────────

def test_auth_always_refuses_an_api_key(monkeypatch):
    # even alongside a valid subscription token: subscription only, never billed API
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    monkeypatch.setenv("ONBOARDING_CLAUDE_SDK_ALLOW_API_KEY", "1")  # the removed opt-in is ignored
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-123")
    with pytest.raises(RuntimeError) as exc:
        resolve_subscription_auth(_sdk_settings())
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_auth_token_passed_through(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-123")
    env = resolve_subscription_auth(dataclasses.replace(_sdk_settings(), claude_sdk_oauth_token="tok-123"))
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-123"


# ── search_code: Cortex's one custom tool ─────────────────────────────────────

class _FakeStore:
    def search(self, namespace, query, k):
        return [{"text": "def login(): ...", "score": 0.9,
                 "metadata": {"path": "src/auth.py", "line_start": 1, "line_end": 2}}]


def test_search_code_tool_runs_on_the_executor_and_records_what_it_showed():
    from cortex.agents.loop import _search_code_tool
    from cortex.agents.tools import SEARCH_CODE_TOOL, ToolExecutor

    ex = ToolExecutor(namespace="x", store=_FakeStore())
    events = []
    t = _search_code_tool(ex, events.append)
    assert t.name == "search_code" and t.input_schema == SEARCH_CODE_TOOL["input_schema"]

    result = asyncio.run(t.handler({"query": "login", "unexpected": "ignored"}))
    assert "[src/auth.py:1-2]" in result["content"][0]["text"]
    assert ex.used_paths == {"src/auth.py"}                       # grounding sees it
    assert ex.call_log == [{"tool": "search_code", "input": {"query": "login", "top_k": 8}}]
    assert [e["type"] for e in events] == ["tool_call", "tool_result"] and events[1]["ok"] is True
