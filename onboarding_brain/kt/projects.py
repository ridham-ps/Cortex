"""Project groups — link several ingested repos into one debuggable unit.

A *project* is just a named set of namespaces (already-ingested repos). The
cross-repo debug agent searches every repo in a project at once, so a symptom
that spans a frontend and its backend can be traced end-to-end.

Storage is a single JSON file at ``<index_dir>/projects.json`` — deliberately
simple and local, mirroring the JSON chat store. All writes are best-effort and
never raise on I/O failure.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import Settings, get_settings
from .store import get_store, slugify


def _projects_file(settings: Settings) -> Path:
    return Path(settings.index_dir) / "projects.json"


def _load_all(settings: Settings) -> dict[str, dict]:
    f = _projects_file(settings)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    return projects if isinstance(projects, dict) else {}


def _save_all(projects: dict[str, dict], settings: Settings) -> None:
    f = _projects_file(settings)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"projects": projects}, indent=2), encoding="utf-8")
    except OSError:
        pass


def _indexed_namespaces(settings: Settings) -> set[str]:
    """The set of namespaces that are actually ingested (have an index)."""
    try:
        store = get_store(settings)
        return {n.get("namespace", "") for n in store.list_namespaces()} - {""}
    except Exception:  # noqa: BLE001
        return set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_project(
    name: str, namespaces: list[str], *, settings: Optional[Settings] = None
) -> dict:
    """Create (or overwrite) a project grouping the given ingested repos.

    Raises ValueError if fewer than two valid, indexed namespaces are supplied —
    a one-repo "project" is just a normal repo, so cross-repo has no meaning.
    """
    settings = settings or get_settings()
    name = (name or "").strip()
    if not name:
        raise ValueError("project name is required")

    indexed = _indexed_namespaces(settings)
    wanted = [slugify(n) for n in namespaces if n and n.strip()]
    # de-dup, preserve order
    seen: set[str] = set()
    resolved = [n for n in wanted if not (n in seen or seen.add(n))]

    missing = [n for n in resolved if n not in indexed]
    if missing:
        raise ValueError(
            "these repos are not indexed yet (ingest them first): " + ", ".join(missing)
        )
    if len(resolved) < 2:
        raise ValueError("a project needs at least 2 indexed repos to link")

    slug = slugify(name)
    projects = _load_all(settings)
    existing = projects.get(slug, {})
    record = {
        "slug": slug,
        "name": name,
        "namespaces": resolved,
        "created_at": existing.get("created_at", _now()),
        "updated_at": _now(),
    }
    projects[slug] = record
    _save_all(projects, settings)
    return record


def list_projects(*, settings: Optional[Settings] = None) -> list[dict]:
    settings = settings or get_settings()
    return sorted(_load_all(settings).values(), key=lambda p: p.get("name", ""))


def get_project(slug: str, *, settings: Optional[Settings] = None) -> Optional[dict]:
    settings = settings or get_settings()
    return _load_all(settings).get(slugify(slug))


def set_project_repos(
    slug: str, namespaces: list[str], *, settings: Optional[Settings] = None
) -> dict:
    """Replace a project's repo set (validated the same way as create)."""
    settings = settings or get_settings()
    proj = get_project(slug, settings=settings)
    if not proj:
        raise KeyError(slug)
    return create_project(proj["name"], namespaces, settings=settings)


def delete_project(slug: str, *, settings: Optional[Settings] = None) -> bool:
    settings = settings or get_settings()
    projects = _load_all(settings)
    if slugify(slug) not in projects:
        return False
    projects.pop(slugify(slug), None)
    _save_all(projects, settings)
    return True
