"""How Cortex connects to Claude — login, locked-down options, async bridge.

Cortex talks to Claude through the Claude Agent SDK (`claude-agent-sdk`). The
SDK starts the bundled Claude Code CLI as a subprocess; that CLI owns the
tool-use loop. Cortex authenticates with your Claude Pro/Max SUBSCRIPTION over
OAuth — no billed API key.

  * `get_claude(settings)` — the connection, after checking the login rules.
  * `ClaudeConnection.base_options()` — the locked-down options every agent run
    starts from: no tools at all, no local settings files. The loop that
    adds the tools (Read/Grep/Glob + search_code) lives in `cortex/agents/loop.py`.
  * `ClaudeConnection.complete()` — one tool-less question/answer. The app does
    not use it; the live RAGAS benchmark uses it as its LLM judge.
  * `run_sync()` — the SDK is async, the app is sync: run a coroutine to
    completion on its own thread with its own event loop.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from cortex.config import Settings, get_settings


def get_claude(settings: Optional[Settings] = None) -> "ClaudeConnection":
    """The Claude connection. Raises RuntimeError if the login rules aren't met."""
    return ClaudeConnection(settings or get_settings())


def resolve_subscription_auth(settings: Settings) -> dict[str, str]:
    """Check the subscription login and return the env for the CLI subprocess.

    Cortex runs ONLY on a Claude Pro/Max subscription (OAuth) — never on an API
    key. The CLI ranks ANTHROPIC_API_KEY above OAuth, so a key in the environment
    would silently switch to billed API usage; that is refused outright.
    """
    if os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is set. Cortex runs only on your Claude subscription login, "
            "and a key would override it and bill the API — unset ANTHROPIC_API_KEY."
        )
    token = settings.claude_sdk_oauth_token or os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not token and not (Path.home() / ".claude" / ".credentials.json").exists():
        raise RuntimeError(
            "no Claude subscription login found. Run `claude setup-token` and set "
            "CLAUDE_CODE_OAUTH_TOKEN, or log in once with `claude`."
        )

    env: dict[str, str] = {}
    if token:
        # Pass the long-lived token through to the CLI subprocess explicitly.
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def run_sync(coro_factory: Callable[[], Any]) -> Any:
    """Run an async coroutine to completion from sync code.

    Always executes in a fresh thread with a private event loop so it works even
    when the calling thread already has a running loop. `coro_factory` is a
    zero-arg callable that builds the coroutine *inside* the worker thread.
    """
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001 — re-raised on the calling thread
            box["error"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


class ClaudeConnection:
    def __init__(self, settings: Settings):
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("`claude-agent-sdk` not installed. pip install claude-agent-sdk") from exc
        self.settings = settings
        self._auth_env = resolve_subscription_auth(settings)  # fail fast on bad login
        self._model = settings.claude_sdk_model  # "" => the subscription's default model

    @property
    def name(self) -> str:
        return f"claude_sdk/{self._model or 'subscription-default'}"

    def base_options(self, **overrides: Any):
        """ClaudeAgentOptions for every run, locked down to a grounded posture."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts: dict[str, Any] = dict(
            model=self._model or None,
            permission_mode="bypassPermissions",  # only our own read-only tools can run
            setting_sources=None,                  # ignore any CLAUDE.md / settings files
            tools=[],                              # disable ALL built-in tools (Read, Bash, ...)
            allowed_tools=[],                      # the agent loop allows only mcp__kt__* tools
            env=dict(self._auth_env),
        )
        opts.update(overrides)
        return ClaudeAgentOptions(**opts)

    def complete(self, system: str, user: str) -> str:
        """One tool-less question → answer text, retried with backoff
        (ONBOARDING_MAX_RETRIES). Used by the RAGAS benchmark's judge."""
        attempts = max(1, self.settings.max_retries + 1)
        last: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                return self._complete_once(system, user)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < attempts - 1:
                    time.sleep(min(2.0, 0.5 * (2 ** attempt)))
        raise RuntimeError(f"{self.name} failed after {attempts} attempt(s): {last}") from last

    def _complete_once(self, system: str, user: str) -> str:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query

        async def _run() -> str:
            final: Optional[str] = None
            text_parts: list[str] = []
            async for msg in query(prompt=user, options=self.base_options(system_prompt=system, max_turns=1)):
                if isinstance(msg, AssistantMessage):
                    if msg.error:
                        raise RuntimeError(f"claude_sdk error: {msg.error}")
                    text_parts += [b.text for b in msg.content if isinstance(b, TextBlock)]
                elif isinstance(msg, ResultMessage):
                    if msg.is_error:
                        raise RuntimeError(f"claude_sdk result error: {msg.errors or msg.subtype}")
                    final = msg.result
            return (final or "".join(text_parts)).strip()

        return run_sync(_run)
