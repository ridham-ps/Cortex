"""Model fallback policy — resilience when the Claude subscription is under pressure.

Two mechanisms, composable:

1. **Reactive ladder** — when a run fails with a *retryable* limit signal
   (rate limit / usage cap / overloaded / transient 5xx / timeout), retry it on
   the next cheaper model in the configured ladder.

2. **Proactive budget routing** — the subscription's real quota is invisible to
   the app, so we keep a LOCAL, per-process rolling-window count of agent
   requests. Once usage crosses `budget_downgrade_at`, new runs start on the
   fast model instead of the primary — heading off the limit before we hit it.

Both are opt-in via config; with nothing configured, behaviour is unchanged.

Note: the counter is per process and approximate (it counts requests, not real
tokens or the account-wide quota). It is a safety heuristic, not an accountant.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

from ..config import Settings

# Rolling window of agent-request timestamps (monotonic seconds).
_LOCK = threading.Lock()
_HITS: deque[float] = deque()

# Substrings that mark a failure worth downgrading for (vs. a real bug we should
# surface). Matched case-insensitively against the recorded error strings.
_LIMIT_MARKERS = (
    "rate_limit", "rate limit", "ratelimit",
    "usage limit", "usage_limit", "quota", "limit reached",
    "overloaded", "capacity", "temporarily unavailable", "temporarily",
    "timeout", "timed out",
    "429", "500", "502", "503", "504", "529",
)


def model_ladder(settings: Settings) -> list[str]:
    """The ordered list of models to try: primary first, then each fallback.

    An entry of "" means "the subscription default model" (primary only).
    """
    ladder: list[str] = [settings.claude_sdk_model]  # "" allowed = default
    for m in settings.claude_sdk_fallbacks:
        if m and m not in ladder:
            ladder.append(m)
    return ladder


def fast_model(settings: Settings, ladder: list[str] | None = None) -> str:
    """The model to route to once the soft budget is crossed."""
    if settings.budget_fast_model:
        return settings.budget_fast_model
    ladder = ladder or model_ladder(settings)
    return ladder[-1] if len(ladder) > 1 else ladder[0]


def _prune(now: float, window_s: int) -> None:
    cutoff = now - window_s
    while _HITS and _HITS[0] < cutoff:
        _HITS.popleft()


def record_request(settings: Settings) -> None:
    """Count one agent request toward the rolling budget window."""
    if settings.soft_budget_per_window <= 0:
        return
    now = time.monotonic()
    with _LOCK:
        _HITS.append(now)
        _prune(now, settings.budget_window_s)


def usage_ratio(settings: Settings) -> float:
    """Fraction of the soft budget used in the current window (0.0 if disabled)."""
    if settings.soft_budget_per_window <= 0:
        return 0.0
    now = time.monotonic()
    with _LOCK:
        _prune(now, settings.budget_window_s)
        n = len(_HITS)
    return n / float(settings.soft_budget_per_window)


def reset_budget() -> None:
    """Clear the rolling window (used by tests)."""
    with _LOCK:
        _HITS.clear()


def proactive_start_index(settings: Settings, ladder: list[str]) -> int:
    """Where to START in the ladder given current budget usage.

    Below the threshold: start at the primary (index 0). At/above it: start at
    the fast model so we don't spend the primary's quota when it's nearly gone.
    """
    if settings.soft_budget_per_window > 0 and usage_ratio(settings) >= settings.budget_downgrade_at:
        fm = fast_model(settings, ladder)
        if fm in ladder:
            return ladder.index(fm)
    return 0


def is_retryable_limit(err: str) -> bool:
    """True if an error string looks like a transient limit worth downgrading for."""
    e = (err or "").lower()
    return any(m in e for m in _LIMIT_MARKERS)


def run_with_fallback(
    settings: Settings,
    attempt: Callable[[str], tuple[str, int, list[str]]],
    *,
    on_downgrade: Callable[[str, str], None] | None = None,
) -> tuple[str, int, list[str], str]:
    """Run `attempt(model)` down the ladder, downgrading on retryable limits.

    `attempt(model)` returns (answer, turns, errors) for that model — where an
    empty `model` means "subscription default". Records one budget request, picks
    the proactive start point, and steps to the next model whenever the current
    attempt reports a retryable limit AND a cheaper model remains.

    Returns (answer, turns, all_errors, effective_model).
    """
    ladder = model_ladder(settings)
    record_request(settings)
    start = proactive_start_index(settings, ladder)

    all_errors: list[str] = []
    answer, turns, effective = "", 1, ladder[start] if ladder else ""
    for idx in range(start, len(ladder)):
        model = ladder[idx]
        answer, turns, errs = attempt(model)
        all_errors.extend(errs)
        effective = model
        limit_hit = any(is_retryable_limit(e) for e in errs)
        if limit_hit and idx + 1 < len(ladder):
            if on_downgrade:
                on_downgrade(model, ladder[idx + 1])
            continue
        break
    return answer, turns, all_errors, effective


def model_label(effective: str) -> str:
    """Human/UI label for the model that actually answered."""
    return f"claude_sdk/{effective or 'subscription-default'}"
