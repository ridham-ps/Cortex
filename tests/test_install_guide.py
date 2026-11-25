"""The install guide (cortex/install_guide.py): reads a repo's README and root
config files and maps them to setup steps and prerequisites. No LLM."""
import shutil

from cortex.config import get_settings
from cortex.install_guide import install_guide, read_repo_setup_files
from tests.fixtures import NODE_REPO, make_repo


def test_reads_readme_and_root_configs():
    repo = make_repo(NODE_REPO)
    try:
        ctx = read_repo_setup_files(str(repo))
        assert ctx["readme"]["file"] == "README.md"
        assert [c["file"] for c in ctx["config_files"]] == ["package.json"]
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_missing_folder_reports_an_error():
    assert "error" in read_repo_setup_files("/no/such/path/xyz")


def test_guide_derives_steps_from_config_files():
    repo = make_repo({"README.md": "# Svc\n\nA FastAPI service.\n",
                      "requirements.txt": "fastapi\nuvicorn\n", "Dockerfile": "FROM python:3.12\n"})
    try:
        g = install_guide({"repo_path": str(repo)}, get_settings())
        assert "Install dependencies: pip install -r requirements.txt" in g["setup_steps"]
        assert any("docker build" in s for s in g["setup_steps"])
        assert any("Python" in p for p in g["prerequisites"])
        assert set(g["sources"]) == {"requirements.txt", "dockerfile"}
    finally:
        shutil.rmtree(repo, ignore_errors=True)
