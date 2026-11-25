"""Install guide — "what do I need to install, and how do I run it?" (no LLM).

Reads the repo's own README and root config files (package.json,
requirements.txt, Dockerfile, ...) and maps them to setup steps and toolchain
prerequisites with fixed rules. It is the only helper that is not an agent.

Served by the small helper registry behind `GET /v1/agents` and
`POST /v1/agents/{id}/run` (`list_agents` / `run_agent` below).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from cortex.config import Settings, get_settings
from cortex.index.access import _check_allowed

# Setup steps keyed by the config file present in the repo.
_SETUP_RULES = [
    ("angular.json", ["Install dependencies: npm install", "Run locally: npx ng serve (or npm start)"]),
    ("package.json", ["Install dependencies: npm install", "Start the app: npm start (see package.json scripts)"]),
    ("requirements.txt", ["Create a virtualenv", "Install dependencies: pip install -r requirements.txt"]),
    ("pyproject.toml", ["Install the package: pip install . (or poetry install)"]),
    ("docker-compose.yml", ["Start everything: docker compose up"]),
    ("dockerfile", ["Build the image: docker build -t app .", "Run it: docker run app"]),
    ("makefile", ["Run the documented make target: make (see Makefile)"]),
    ("go.mod", ["Build: go build ./...", "Run: go run ."]),
    ("cargo.toml", ["Build & run: cargo run"]),
    ("pom.xml", ["Build: mvn install", "Run: mvn spring-boot:run (or per pom.xml)"]),
]

# Toolchain prerequisites a newcomer must install first, by detected stack signal.
_PREREQ = [
    (r"\bangular\b", "Node.js 18 LTS and npm (Angular CLI: `npm i -g @angular/cli`)"),
    (r"\breact|next\b", "Node.js 18+ and npm or yarn"),
    (r"\bvue\b", "Node.js 18+ and npm"),
    (r"\bfastapi|uvicorn|flask|django\b", "Python 3.9+ with pip and a virtualenv"),
    (r"\bspring|maven|gradle\b", "JDK 17+ and Maven/Gradle"),
    (r"\bgo\b|go\.mod", "Go 1.20+"),
    (r"\bcargo|rust\b", "Rust toolchain (rustup)"),
    (r"\bdocker|compose\b", "Docker Desktop (or Docker Engine + compose)"),
    (r"\bpostgres|mysql|mongo|redis\b", "the database server it connects to, running locally or in Docker"),
]

# what we read from the repo root
_README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md")
_CONFIG_NAMES = (
    "package.json", "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "Makefile",
    ".env.example", "angular.json", "pom.xml", "build.gradle", "go.mod",
    "Cargo.toml", "Gemfile", "composer.json", "tsconfig.json", "vite.config.ts",
    "next.config.js", "tox.ini",
)
_MAX_FILE_BYTES = 6000


def read_repo_setup_files(repo_path: str) -> dict[str, Any]:
    """The README and root config files of a local repo, each capped at 6 KB."""
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        return {"error": f"not a directory: {repo}", "repo_path": str(repo)}
    readme = next(({"file": n, "content": _read_text(repo / n)}
                   for n in _README_NAMES if (repo / n).is_file()), None)
    configs = [{"file": n, "content": _read_text(repo / n)}
               for n in _CONFIG_NAMES if (repo / n).is_file()]
    return {"repo_path": str(repo), "readme": readme, "config_files": configs}


def _read_text(path: Path) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[:_MAX_FILE_BYTES]


def install_guide(req: dict, settings: Settings) -> dict:
    """Setup steps + toolchain prerequisites from the repo's own config files."""
    repo_path = req["repo_path"]
    _check_allowed(repo_path, settings)
    ctx = read_repo_setup_files(repo_path)
    if ctx.get("error"):
        raise ValueError(ctx["error"])

    configs = ctx["config_files"]
    config_names = {c["file"].lower() for c in configs}
    readme = (ctx.get("readme") or {}).get("content", "")
    # scan config contents + README + the config FILE NAMES (an empty
    # angular.json still signals Angular) for stack signals
    blob = (" ".join(c["content"] for c in configs) + " " + readme
            + " " + " ".join(config_names)).lower()

    steps, sources = [], []
    for fname, fsteps in _SETUP_RULES:
        if fname in config_names:
            steps.extend(fsteps)
            sources.append(fname)

    prereq: list[str] = []
    for pat, need in _PREREQ:
        if re.search(pat, blob) and need not in prereq:
            prereq.append(need)
    if not prereq:
        prereq = ["Check the README/config files for the required runtime and version."]

    return {
        "agent_id": "installation-guide",
        "name": "Installation Guide",
        "repo_path": ctx["repo_path"],
        "prerequisites": prereq,
        "setup_steps": steps or ["not found in repo"],
        "sources": sources,
        "validation_status": "passed",
    }


# ── the helper registry behind /v1/agents ────────────────────────────────────

@dataclass(frozen=True)
class HelperSpec:
    id: str           # machine name, used in the URL: /v1/agents/<id>/run
    name: str         # display name for people
    category: str
    description: str
    run: Callable[[dict, Settings], dict]


HELPERS: dict[str, HelperSpec] = {
    s.id: s for s in [
        HelperSpec("installation-guide", "Installation Guide", "Enablement",
                   "Exact local-setup steps from the real config files, plus the toolchain "
                   "prerequisites (runtimes + versions) you must install first.",
                   install_guide),
    ]
}


def list_agents(settings: Optional[Settings] = None) -> list[dict[str, Any]]:
    s = settings or get_settings()
    return [{"agent_id": h.id, "name": h.name, "category": h.category,
             "description": h.description, "model_used": s.model_used} for h in HELPERS.values()]


def run_agent(agent_id: str, req: dict, *, settings: Optional[Settings] = None) -> dict:
    spec = HELPERS.get(agent_id)
    if spec is None:
        raise KeyError(agent_id)
    return spec.run(req, settings or get_settings())
