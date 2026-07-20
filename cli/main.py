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

from onboarding_brain import AGENT_ID, __version__
from onboarding_brain.config import get_settings

app = typer.Typer(add_completion=False, help="Onboarding Brain — index any local repo and ask grounded questions")


@app.command()
def ingest(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Repo to index"),
    namespace: str = typer.Option(None, "--namespace", "-n", help="Index name (defaults to repo folder)"),
    rebuild: bool = typer.Option(False, help="Re-index even if already indexed"),
):
    """Index a repo into the knowledge base (vector store)."""
    from onboarding_brain.contract import IngestRequest
    from onboarding_brain.kt.ingest import ingest_repo

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
    from onboarding_brain.contract import AskRequest
    from onboarding_brain.kt.chat import ask as kt_ask

    try:
        resp = kt_ask(AskRequest(namespace=namespace, question=question), settings=get_settings())
    except ValueError as exc:
        typer.echo(json.dumps({"error": "ask_failed", "detail": str(exc)}), err=True)
        raise typer.Exit(code=2)
    typer.echo(json.dumps(resp.model_dump(), indent=2, ensure_ascii=False, default=str))


@app.command()
def namespaces():
    """List indexed repos."""
    from onboarding_brain.kt.store import get_store
    typer.echo(json.dumps(get_store(get_settings()).list_namespaces(), indent=2, default=str))


@app.command("project-create")
def project_create(
    name: str = typer.Argument(..., help="Project name"),
    repos: list[str] = typer.Argument(..., help="Two or more indexed repo names to link"),
):
    """Link 2+ indexed repos into a project for cross-repo debugging."""
    from onboarding_brain.kt import projects as P

    try:
        proj = P.create_project(name, list(repos), settings=get_settings())
    except ValueError as exc:
        typer.echo(json.dumps({"error": "project_create_failed", "detail": str(exc)}), err=True)
        raise typer.Exit(code=2)
    typer.echo(json.dumps(proj, indent=2, ensure_ascii=False, default=str))


@app.command()
def projects():
    """List cross-repo projects."""
    from onboarding_brain.kt import projects as P
    typer.echo(json.dumps(P.list_projects(settings=get_settings()), indent=2, default=str))


@app.command()
def debug(
    symptom: str = typer.Argument(..., help="The problem/symptom to root-cause"),
    project: str = typer.Option(..., "--project", "-p", help="Project name/slug to debug"),
):
    """Trace a symptom across all repos in a project and report the likely cause."""
    from onboarding_brain.contract import AskRequest
    from onboarding_brain.kt import projects as P
    from onboarding_brain.kt.agent_sdk import agent_debug_sdk

    settings = get_settings()
    proj = P.get_project(project, settings=settings)
    if not proj:
        typer.echo(json.dumps({"error": "project_not_found", "detail": project}), err=True)
        raise typer.Exit(code=2)
    try:
        resp = agent_debug_sdk(
            AskRequest(namespace=proj["slug"], question=symptom),
            proj["namespaces"], project_slug=proj["slug"], settings=settings)
    except (ValueError, TypeError, RuntimeError) as exc:
        typer.echo(json.dumps({"error": "debug_failed", "detail": str(exc)}), err=True)
        raise typer.Exit(code=2)
    typer.echo(json.dumps(resp.model_dump(), indent=2, ensure_ascii=False, default=str))


@app.command()
def info():
    """Print agent + backend configuration."""
    s = get_settings()
    typer.echo(json.dumps({
        "agent_id": AGENT_ID, "version": __version__,
        "backend": s.backend, "model_used": s.model_used,
    }, indent=2))


if __name__ == "__main__":
    app()
