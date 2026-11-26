import os
import shutil

import pytest

os.environ["ONBOARDING_API_KEYS"] = "test-key"
# high enough that the whole module's requests don't trip the limiter; the 429
# behaviour itself is unit-tested directly against _rate_limit with a small cap
os.environ["ONBOARDING_RATE_LIMIT_PER_MIN"] = "500"

try:
    from fastapi.testclient import TestClient

    from api.server import app

    client = TestClient(app)
    _HAVE = True
except Exception:
    _HAVE = False

pytestmark = pytest.mark.skipif(not _HAVE, reason="fastapi not installed")
from tests.fixtures import NODE_REPO, make_repo  # noqa: E402

AUTH = {"Authorization": "Bearer test-key"}
AGENT = "installation-guide"


def test_home_serves_ui():
    r = client.get("/")
    assert r.status_code == 200 and "<title>" in r.text


def test_health_and_catalog():
    from cortex import AGENT_ID
    assert client.get("/health").json()["agent_id"] == AGENT_ID
    assert AGENT in {a["agent_id"] for a in client.get("/v1/agents").json()["agents"]}


def test_run_requires_auth():
    assert client.post(f"/v1/agents/{AGENT}/run", json={"repo_path": "."}).status_code == 401


def test_agent_registry_lists_install_guide():
    ids = {a["agent_id"] for a in client.get("/v1/agents").json()["agents"]}
    assert "installation-guide" in ids


def test_installation_agent_runs():
    repo = make_repo(NODE_REPO)
    try:
        r = client.post("/v1/agents/installation-guide/run", json={"repo_path": str(repo)}, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["agent_id"] == "installation-guide"
        assert body["prerequisites"] and isinstance(body["prerequisites"], list)
        assert body["setup_steps"] and isinstance(body["setup_steps"], list)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_unknown_agent_404_registry():
    assert client.post("/v1/agents/nope-zzz/run", json={"repo_path": "."}, headers=AUTH).status_code == 404


def test_unknown_agent_404():
    assert client.post("/v1/agents/nope/run", json={"repo_path": "."}, headers=AUTH).status_code == 404


def test_invalid_request_422():
    assert client.post(f"/v1/agents/{AGENT}/run", content=b"{bad", headers=AUTH).status_code == 422


def test_bad_repo_path_400():
    r = client.post(f"/v1/agents/{AGENT}/run", json={"repo_path": "/no/such/dir/zzz"}, headers=AUTH)
    assert r.status_code == 400


def test_ingest_ask_namespaces_flow(fake_agent):
    repo = make_repo({"README.md": "# X\n\nApp.\n", "src/auth.py": "def login(u,p): return verify(u,p)\n"})
    try:
        ing = client.post("/v1/ingest", json={"repo_path": str(repo), "namespace": "apitest", "rebuild": True}, headers=AUTH)
        assert ing.status_code == 200 and ing.json()["chunks_indexed"] >= 1

        nss = client.get("/v1/namespaces", headers=AUTH).json()["namespaces"]
        assert any(n["namespace"] == "apitest" for n in nss)

        fake_agent.reads = ["src/auth.py"]
        fake_agent.reply = {"found": True, "answer": "`login()` in `src/auth.py` delegates to `verify(u, p)`.",
                            "sources": [{"path": "src/auth.py", "line_start": 1, "line_end": 1}]}
        ask = client.post("/v1/ask", json={"namespace": "apitest", "question": "how does login work?"}, headers=AUTH)
        assert ask.status_code == 200
        body = ask.json()
        assert body["trace"]["agent_id"] == "kt-agent-v1"
        assert [s["path"] for s in body["sources"]] == ["src/auth.py"]
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_ask_unindexed_400():
    r = client.post("/v1/ask", json={"namespace": "nope-zzz", "question": "x"}, headers=AUTH)
    assert r.status_code == 400


def test_annotations_endpoints():
    repo = make_repo({"README.md": "# X\n\nApp.\n",
                      "src/XfrmSvc.ts": "export class XfrmSvc { run() { return XfrmSvc.go(); } }\n"})
    try:
        client.post("/v1/ingest", json={"repo_path": str(repo), "namespace": "gapapi", "rebuild": True}, headers=AUTH)
        assert client.get("/v1/gaps/gapapi", headers=AUTH).status_code == 404  # gap finder removed
        save = client.post("/v1/annotations/gapapi",
                           json={"file": "src/XfrmSvc.ts", "answer": "Billing reconciliation engine.",
                                 "symbol": "class XfrmSvc"}, headers=AUTH)
        assert save.status_code == 200
        anns = client.get("/v1/annotations/gapapi", headers=AUTH).json()["annotations"]
        assert any("Billing" in a["answer"] for a in anns)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_rate_limit_blocks_after_limit():
    # the sliding-window limiter raises 429 once a key exceeds its cap
    from fastapi import HTTPException

    from api.server import _rate_limit
    key = "rl-test-" + os.urandom(4).hex()
    for _ in range(5):
        _rate_limit(key, 5)  # 5 allowed in the window
    with pytest.raises(HTTPException) as ei:
        _rate_limit(key, 5)  # 6th -> blocked
    assert ei.value.status_code == 429


def test_oversize_request_413():
    # body over max_request_bytes (16 KB default) is rejected before parsing
    big = {"repo_path": "/x" * 9000}  # ~18 KB
    r = client.post(f"/v1/agents/{AGENT}/run", json=big, headers=AUTH)
    assert r.status_code == 413


def test_file_endpoint_returns_full_file():
    repo = make_repo({"README.md": "# X\n\nApp.\n",
                      "src/auth.py": "def login(u, p):\n    return verify(u, p)\n"})
    try:
        client.post("/v1/ingest", json={"repo_path": str(repo), "namespace": "fileapi", "rebuild": True}, headers=AUTH)
        r = client.get("/v1/file/fileapi", params={"path": "src/auth.py"}, headers=AUTH)
        assert r.status_code == 200 and "def login" in r.json()["content"]
        assert client.get("/v1/file/fileapi", params={"path": "missing.py"}, headers=AUTH).status_code == 404
    finally:
        shutil.rmtree(repo, ignore_errors=True)


TOUR_REPO = {"README.md": "# X\n\nApp.\n",
             "app/server.py": "from fastapi import FastAPI\nfrom .config import s\napp = FastAPI()\nif __name__ == '__main__':\n    import uvicorn; uvicorn.run(app)\n",
             "app/config.py": "s = {}\n"}


def test_tour_endpoint_contract_and_cache(fake_agent):
    repo = make_repo(TOUR_REPO)
    try:
        client.post("/v1/ingest", json={"repo_path": str(repo), "namespace": "tourapi", "rebuild": True}, headers=AUTH)
        fake_agent.reads = ["app/server.py", "app/config.py"]
        fake_agent.reply = {"overview": "A FastAPI app.", "entry_point": "app/server.py", "chapters": [
            {"title": "Startup", "why": "Where it begins.", "stops": [
                {"path": "app/server.py", "reason": "entry", "insight": "Creates the app."},
                {"path": "app/config.py", "reason": "settings", "insight": "Holds config."}]}]}
        r = client.get("/v1/tour/tourapi", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        # matches the TourResponse contract
        assert {"namespace", "entry_point", "total_stops", "chapters"} <= set(body)
        assert body["entry_point"] == "app/server.py" and body["total_stops"] == 2
        # second call is served from tour.json without running the agent again
        runs = len(fake_agent.system_prompts)
        assert client.get("/v1/tour/tourapi", headers=AUTH).status_code == 200
        assert len(fake_agent.system_prompts) == runs
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_tour_endpoint_reports_agent_failure_as_503(fake_agent):
    repo = make_repo(TOUR_REPO)
    try:
        client.post("/v1/ingest", json={"repo_path": str(repo), "namespace": "tourfail", "rebuild": True}, headers=AUTH)
        fake_agent.reply = "no JSON at all"
        r = client.get("/v1/tour/tourfail", headers=AUTH)
        assert r.status_code == 503 and "tour agent" in r.json()["error"]
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_chat_history_endpoints(fake_agent):
    repo = make_repo({"README.md": "# X\n\nApp.\n", "src/auth.py": "def login(u,p): return verify(u,p)\n"})
    try:
        client.post("/v1/ingest", json={"repo_path": str(repo), "namespace": "histapi", "rebuild": True}, headers=AUTH)
        fake_agent.reads = ["src/auth.py"]
        fake_agent.reply = {"found": True, "answer": "`login()` lives in `src/auth.py`.",
                            "sources": [{"path": "src/auth.py", "line_start": 1, "line_end": 1}]}
        client.post("/v1/ask", json={"namespace": "histapi", "question": "how does login work?"}, headers=AUTH)
        turns = client.get("/v1/chat/histapi", headers=AUTH).json()["turns"]
        assert len(turns) == 2 and turns[0]["role"] == "user"
        assert client.delete("/v1/chat/histapi", headers=AUTH).json()["cleared"] is True
        assert client.get("/v1/chat/histapi", headers=AUTH).json()["turns"] == []
    finally:
        shutil.rmtree(repo, ignore_errors=True)
