"""Model fallback policy: ladder, budget routing, retryable detection, orchestration."""
from onboarding_brain.config import get_settings
from onboarding_brain.kt import model_policy as MP


def _settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return get_settings()


def test_model_ladder_and_fast_model(monkeypatch):
    s = _settings(monkeypatch, CLAUDE_SDK_MODEL="claude-opus-4-8",
                  CLAUDE_SDK_FALLBACKS="claude-sonnet-5, claude-haiku-4-5-20251001")
    assert MP.model_ladder(s) == ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"]
    assert MP.fast_model(s) == "claude-haiku-4-5-20251001"


def test_ladder_dedupes_and_defaults(monkeypatch):
    s = _settings(monkeypatch, CLAUDE_SDK_MODEL="", CLAUDE_SDK_FALLBACKS="")
    assert MP.model_ladder(s) == [""]              # subscription default, no ladder
    assert MP.fast_model(s) == ""


def test_explicit_fast_model_wins(monkeypatch):
    s = _settings(monkeypatch, CLAUDE_SDK_MODEL="claude-opus-4-8",
                  CLAUDE_SDK_FALLBACKS="claude-sonnet-5",
                  ONBOARDING_BUDGET_FAST_MODEL="claude-haiku-4-5-20251001")
    assert MP.fast_model(s) == "claude-haiku-4-5-20251001"


def test_is_retryable_limit():
    assert MP.is_retryable_limit("sdk_error:rate_limit_error")
    assert MP.is_retryable_limit("sdk_result_error:overloaded")
    assert MP.is_retryable_limit("usage limit reached")
    assert MP.is_retryable_limit("agent_sdk_failed:HTTP 529 capacity")
    # a real bug is NOT a downgrade trigger
    assert not MP.is_retryable_limit("KeyError: 'username'")
    assert not MP.is_retryable_limit("sdk_result_error:tool_use_failed")
    assert not MP.is_retryable_limit("")


def test_budget_ratio_and_proactive_start(monkeypatch):
    s = _settings(monkeypatch, CLAUDE_SDK_MODEL="claude-opus-4-8",
                  CLAUDE_SDK_FALLBACKS="claude-haiku-4-5-20251001",
                  ONBOARDING_SOFT_BUDGET_PER_WINDOW="4",
                  ONBOARDING_BUDGET_DOWNGRADE_AT="0.8")
    MP.reset_budget()
    ladder = MP.model_ladder(s)
    for _ in range(3):
        MP.record_request(s)
    assert MP.usage_ratio(s) == 0.75
    assert MP.proactive_start_index(s, ladder) == 0        # below threshold -> primary
    MP.record_request(s)                                   # now 4/4 = 1.0
    assert MP.usage_ratio(s) >= 0.8
    assert MP.proactive_start_index(s, ladder) == 1        # -> the fast (haiku) index
    MP.reset_budget()


def test_budget_disabled_by_default(monkeypatch):
    s = _settings(monkeypatch, ONBOARDING_SOFT_BUDGET_PER_WINDOW="0")
    MP.reset_budget()
    MP.record_request(s)
    assert MP.usage_ratio(s) == 0.0
    assert MP.proactive_start_index(s, MP.model_ladder(s)) == 0


def test_run_with_fallback_downgrades_on_limit(monkeypatch):
    s = _settings(monkeypatch, CLAUDE_SDK_MODEL="claude-opus-4-8",
                  CLAUDE_SDK_FALLBACKS="claude-sonnet-5, claude-haiku-4-5-20251001",
                  ONBOARDING_SOFT_BUDGET_PER_WINDOW="0")
    MP.reset_budget()
    switches = []

    def attempt(model):
        if model == "claude-opus-4-8":
            return ("", 1, ["sdk_error:rate_limit_error"])   # limited -> downgrade
        return (f"answer via {model}", 2, [])                # sonnet succeeds

    answer, turns, errs, effective = MP.run_with_fallback(
        s, attempt, on_downgrade=lambda a, b: switches.append((a, b)))
    assert effective == "claude-sonnet-5"
    assert answer == "answer via claude-sonnet-5"
    assert switches == [("claude-opus-4-8", "claude-sonnet-5")]
    assert any("rate_limit" in e for e in errs)


def test_run_with_fallback_does_not_downgrade_on_real_error(monkeypatch):
    s = _settings(monkeypatch, CLAUDE_SDK_MODEL="claude-opus-4-8",
                  CLAUDE_SDK_FALLBACKS="claude-haiku-4-5-20251001",
                  ONBOARDING_SOFT_BUDGET_PER_WINDOW="0")
    MP.reset_budget()
    calls = []

    def attempt(model):
        calls.append(model)
        return ("partial answer", 1, ["KeyError: boom"])     # a real bug, not a limit

    answer, turns, errs, effective = MP.run_with_fallback(s, attempt)
    assert effective == "claude-opus-4-8"       # stayed on primary
    assert calls == ["claude-opus-4-8"]         # did not try the fallback
    assert answer == "partial answer"


def test_run_with_fallback_starts_on_fast_when_over_budget(monkeypatch):
    s = _settings(monkeypatch, CLAUDE_SDK_MODEL="claude-opus-4-8",
                  CLAUDE_SDK_FALLBACKS="claude-haiku-4-5-20251001",
                  ONBOARDING_SOFT_BUDGET_PER_WINDOW="2",
                  ONBOARDING_BUDGET_DOWNGRADE_AT="0.8")
    MP.reset_budget()
    MP.record_request(s)
    MP.record_request(s)                        # 2/2 -> over threshold before the run
    calls = []

    def attempt(model):
        calls.append(model)
        return (f"answer via {model}", 1, [])

    answer, turns, errs, effective = MP.run_with_fallback(s, attempt)
    assert calls == ["claude-haiku-4-5-20251001"]   # skipped opus entirely
    assert effective == "claude-haiku-4-5-20251001"
    MP.reset_budget()


def test_model_label():
    assert MP.model_label("claude-haiku-4-5-20251001") == "claude_sdk/claude-haiku-4-5-20251001"
    assert MP.model_label("") == "claude_sdk/subscription-default"
