"""Onboarding Brain CLI.

    python -m cli.main ingest --repo . -n myrepo
    python -m cli.main ask "How is login handled?" -n myrepo
    python -m cli.main info
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

# Windows consoles default to cp1252, which crashes on Unicode in code
# snippets (arrows, box-drawing, emoji). Force UTF-8, degrade lossily if not.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass

from cortex import AGENT_ID, __version__
from cortex.config import get_settings

app = typer.Typer(add_completion=False, help="Onboarding Brain — index any local repo and ask grounded questions")


@app.command()
def ingest(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Repo to index"),
    namespace: str = typer.Option(None, "--namespace", "-n", help="Index name (defaults to repo folder)"),
    rebuild: bool = typer.Option(False, help="Re-index even if already indexed"),
):
    """Index a repo into the knowledge base (vector store)."""
    from cortex.contract import IngestRequest
    from cortex.index.ingest import ingest_repo

    try:
        resp = ingest_repo(IngestRequest(repo_path=str(repo), namespace=namespace, rebuild=rebuild),
                           settings=get_settings())
    except ValueError as exc:
        typer.echo(json.dumps({"error": "ingest_failed", "detail": str(exc)}), err=True)
        raise typer.Exit(code=2)
    typer.echo(json.dumps(resp.model_dump(), indent=2, ensure_ascii=False, default=str))


@app.command()
def ask(
    question: str = typer.Argument(..., help="Your question about the codebase"),
    namespace: str = typer.Option(..., "--namespace", "-n", help="Indexed repo name"),
):
    """Ask the KT chatbot a question about an indexed repo."""
    from cortex.contract import AskRequest
    from cortex.agents.chat import ask as chat_ask

    try:
        resp = chat_ask(AskRequest(namespace=namespace, question=question), settings=get_settings())
    except ValueError as exc:
        typer.echo(json.dumps({"error": "ask_failed", "detail": str(exc)}), err=True)
        raise typer.Exit(code=2)
    typer.echo(json.dumps(resp.model_dump(), indent=2, ensure_ascii=False, default=str))


@app.command()
def namespaces():
    """List indexed repos."""
    from cortex.index.store import get_store
    typer.echo(json.dumps(get_store(get_settings()).list_namespaces(), indent=2, default=str))


@app.command()
def info():
    """Print the agent configuration."""
    typer.echo(json.dumps({
        "agent_id": AGENT_ID, "version": __version__, "backend": "claude_sdk",
        "model_used": get_settings().model_used,
    }, indent=2))


if __name__ == "__main__":
    app()
