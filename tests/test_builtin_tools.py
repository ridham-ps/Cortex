"""Builtin tool mode (cortex/agents/loop.py): Claude Code's Read / Grep / Glob,
fenced to the repo folder by hooks — plus kept clones so the files exist on disk."""
import asyncio
import json
import shutil
import subprocess

import pytest

from cortex.agents import loop
from cortex.agents.tools import ToolExecutor
from cortex.contract import IngestRequest
from cortex.index.ingest import ingest_repo, repo_folder
from cortex.index.store import get_store
from tests.fixtures import make_repo


def test_fence_allows_the_repo_and_refuses_everything_else(tmp_path):
    root = tmp_path.resolve()
    (root / "src").mkdir()
    ok = loop._fence_violation
    assert ok("Read", {"file_path": str(root / "src" / "app.py")}, root) is None
    assert ok("Read", {"file_path": "src/app.py"}, root) is None            # relative to the repo
    assert ok("Grep", {"pattern": "def main", "path": "src"}, root) is None
    assert ok("Glob", {"pattern": "**/*.py"}, root) is None
    assert ok("Grep", {"pattern": "x"}, root) is None                      # defaults to the repo

    assert ok("Read", {"file_path": str(root.parent / "secret.txt")}, root)  # outside
    assert ok("Read", {"file_path": "../secret.txt"}, root)                  # escape via ..
    assert ok("Read", {"file_path": ".git/config"}, root)                    # git internals
    assert ok("Grep", {"pattern": "x", "path": str(root.parent)}, root)
    assert ok("Glob", {"pattern": "../**/*.env"}, root)
    assert ok("Glob", {"pattern": "C:/Users/**"}, root)
    assert ok("Read", {}, root)                                               # no path


def test_hooks_deny_outside_calls_and_record_what_was_seen(tmp_path):
    root = tmp_path.resolve()
    ex = ToolExecutor(namespace="x", store=None)
    events = []
    known = {"src/app.py", "src/util.py", "README.md"}
    pre, post = loop._fence_hooks(root, known, ex, events.append)

    denied = asyncio.run(pre({"tool_name": "Read", "tool_input": {"file_path": "../x"}}, None, {}))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    assert asyncio.run(pre({"tool_name": "Read", "tool_input": {"file_path": "src/app.py"}}, None, {})) == {}
    asyncio.run(post({"tool_name": "Read", "tool_input": {"file_path": str(root / "src" / "app.py")},
                      "tool_response": {"file": {"content": "..."}}}, None, {}))
    asyncio.run(post({"tool_name": "Grep", "tool_input": {"pattern": "util"},
                      "tool_response": {"filenames": [str(root / "src" / "util.py")]}}, None, {}))

    assert ex.used_paths == {"src/app.py", "src/util.py"}
    assert [c["tool"] for c in ex.call_log] == ["Read", "Read"] and "denied" in ex.call_log[0]
    # the UI's progress labels get the names it knows
    assert [e["tool"] for e in events if e["type"] == "tool_call"] == ["read_file"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_clone_is_kept_and_repo_folder_points_to_it(tmp_path):
    src = make_repo({"README.md": "# Kept\n", "app/main.py": "print('hi')\n"})
    try:
        for cmd in (["init", "-q"], ["add", "."],
                    ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"]):
            subprocess.run(["git", "-C", str(src), *cmd], check=True, capture_output=True)
        r = ingest_repo(IngestRequest(clone_url=str(src), namespace="keptclone", rebuild=True))
        folder = repo_folder("keptclone")
        assert (folder / "app" / "main.py").is_file()       # the files stay on disk
        assert str(folder) == r.repo_path
        meta = json.loads((get_store().ns_dir("keptclone") / "meta.json").read_text(encoding="utf-8"))
        assert meta["repo_path"] == str(folder)
        assert "kt_idx_" in str(folder)                     # tests keep clones in a temp dir
    finally:
        shutil.rmtree(src, ignore_errors=True)


def test_repo_folder_explains_missing_files(tmp_path):
    repo = make_repo({"README.md": "# Gone\n", "a.py": "x = 1\n"})
    ingest_repo(IngestRequest(repo_path=str(repo), namespace="gonerepo", rebuild=True))
    shutil.rmtree(repo, ignore_errors=True)
    with pytest.raises(ValueError, match="no longer on disk"):
        repo_folder("gonerepo")
