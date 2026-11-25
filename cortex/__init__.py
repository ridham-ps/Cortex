"""Cortex — grounded codebase Q&A, guided tours and
walkthroughs for engineers new to a repository, built on the Claude Agent SDK.
"""
# Trust the OS certificate store before any HTTPS client is built (handles
# corporate TLS-inspection proxies). Imported for side effect.
from cortex import _tls  # noqa: E402,F401

__version__ = "1.0.0"
AGENT_ID = "onboarding-brain"
