"""Cross-repo debug engine: project groups + MultiRepoExecutor (offline)."""
import shutil

import pytest

from onboarding_brain.config import get_settings
from onboarding_brain.contract import IngestRequest
from onboarding_brain.kt import projects as P
from onboarding_brain.kt.agent import _sources_from_multi
from onboarding_brain.kt.ingest import ingest_repo
from onboarding_brain.kt.store import get_store
from onboarding_brain.kt.tools import MultiRepoExecutor, split_qualified_path
from tests.fixtures import make_repo

# A frontend that calls the backend, and a backend with the matching route.
WEB_REPO = {
    "README.md": "# Acme Web\n\nCustomer portal.\n",
    "src/LoginForm.tsx": (
        "export function LoginForm() {\n"
        "  // posts credentials to the auth API\n"
        "  return fetch('/api/login', { method: 'POST' });\n"
        "}\n"
    ),
    "package.json": '{"name":"acme-web","dependencies":{"react":"^18"}}\n',
}
API_REPO = {
    "README.md": "# Acme API\n\nBackend service.\n",
    "app/routes/auth.py": (
        "def login(request):\n"
        "    user = request.json['username']\n"
        "    return authenticate(user)\n"
    ),
    "requirements.txt": "fastapi\nuvicorn\n",
}


def _ingest(files: dict, ns: str):
    settings = get_settings()
    repo = make_repo(files)
    try:
        ingest_repo(IngestRequest(repo_path=str(repo), namespace=ns, rebuild=True),
                    settings=settings)
    finally:
        shutil.rmtree(repo, ignore_errors=True)
    return settings


def test_split_qualified_path():
    ns = ["web", "api"]
    assert split_qualified_path("web/src/a.ts", ns) == ("web", "src/a.ts")
    assert split_qualified_path("api/app/x.py", ns) == ("api", "app/x.py")
    assert split_qualified_path("src/a.ts", ns) == (None, "src/a.ts")
    # longest namespace wins when one is a prefix of another
    assert split_qualified_path("web-admin/x.ts", ["web", "web-admin"]) == ("web-admin", "x.ts")


def test_project_crud_and_validation():
    s = _ingest(WEB_REPO, "webcrud")
    _ingest(API_REPO, "apicrud")

    proj = P.create_project("Acme Stack", ["webcrud", "apicrud"], settings=s)
    assert proj["slug"] == "acme-stack"
    assert set(proj["namespaces"]) == {"webcrud", "apicrud"}

    assert any(p["slug"] == "acme-stack" for p in P.list_projects(settings=s))
    assert P.get_project("acme-stack", settings=s)["name"] == "Acme Stack"

    assert P.delete_project("acme-stack", settings=s) is True
    assert P.get_project("acme-stack", settings=s) is None
    assert P.delete_project("acme-stack", settings=s) is False


def test_project_rejects_single_or_unindexed_repo():
    s = _ingest(WEB_REPO, "solorepo")
    with pytest.raises(ValueError):
        P.create_project("Solo", ["solorepo"], settings=s)          # needs >= 2
    with pytest.raises(ValueError):
        P.create_project("Ghost", ["solorepo", "ghost404"], settings=s)  # not indexed


def test_multi_executor_fans_out_and_qualifies_paths():
    s = _ingest(WEB_REPO, "webfan")
    _ingest(API_REPO, "apifan")
    ex = MultiRepoExecutor(["webfan", "apifan"], get_store(s))

    out = ex.execute("search_code", {"query": "login", "top_k": 5})
    # results are grouped by repo, and BOTH repos surface a login file
    assert "### repo: webfan" in out
    assert "### repo: apifan" in out
    # every recorded path is qualified with its repo
    assert ex.used_paths
    assert all(p.startswith(("webfan/", "apifan/")) for p in ex.used_paths)


def test_multi_executor_routes_read_file_to_owning_repo():
    s = _ingest(WEB_REPO, "webread")
    _ingest(API_REPO, "apiread")
    ex = MultiRepoExecutor(["webread", "apiread"], get_store(s))

    out = ex.execute("read_file", {"path": "LoginForm.tsx"})
    assert "### repo: webread" in out
    assert "LoginForm" in out
    assert "webread/src/LoginForm.tsx" in ex.used_paths
    # the backend file is reachable too, via its own repo
    out2 = ex.execute("read_file", {"path": "auth.py"})
    assert "### repo: apiread" in out2


def test_cross_repo_planted_bug_is_retrievable():
    """The whole point of cross-repo debug: a symptom in the frontend must be able
    to surface the *backend* file that actually causes it. The frontend posts
    `username`, the backend reads `user` — a contract mismatch. Searching the
    symptom across repos must reach the backend auth handler.
    """
    web = {
        "src/LoginForm.tsx": (
            "export function LoginForm() {\n"
            "  // sends the username/password to the auth endpoint\n"
            "  return fetch('/api/login', {\n"
            "    method: 'POST',\n"
            "    body: JSON.stringify({ username, password }),\n"
            "  });\n"
            "}\n"
        ),
        "package.json": '{"name":"web"}\n',
    }
    api = {
        "app/auth_handler.py": (
            "def handle_login(request):\n"
            "    # BUG: frontend sends 'username', not 'user' -> KeyError -> 500\n"
            "    user = request.json['user']\n"
            "    return authenticate(user)\n"
        ),
        "requirements.txt": "flask\n",
    }
    s = _ingest(web, "bugweb")
    _ingest(api, "bugapi")
    ex = MultiRepoExecutor(["bugweb", "bugapi"], get_store(s))

    out = ex.execute("search_code", {"query": "login username payload", "top_k": 6})
    # the backend handler (the real cause) is reachable via cross-repo search
    assert "auth_handler.py" in out
    assert "### repo: bugapi" in out
    # and the frontend caller shows up too, so the agent can trace the flow
    assert "LoginForm.tsx" in out
    assert any(p.startswith("bugapi/") for p in ex.used_paths)


def test_sources_from_multi_tags_each_source_with_repo():
    s = _ingest(WEB_REPO, "websrc")
    _ingest(API_REPO, "apisrc")
    store = get_store(s)
    ex = MultiRepoExecutor(["websrc", "apisrc"], store)
    ex.execute("search_code", {"query": "login"})

    sources = _sources_from_multi(ex, store, ["websrc", "apisrc"])
    assert sources, "expected cross-repo sources"
    assert all(src.repo in ("websrc", "apisrc") for src in sources)
    # display path is the in-repo path, not the qualified one
    assert all(not src.path.startswith(("websrc/", "apisrc/")) for src in sources)
