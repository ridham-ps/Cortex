"""The engine under the agents: chunking, the search index, ingest, wiring, chat
history storage and annotations. No agent runs here (see test_agents.py)."""
import shutil
from pathlib import Path

from cortex.config import get_settings
from cortex.contract import IngestRequest
from cortex.index.chunker import iter_chunks
from cortex.index.ingest import ingest_repo
from cortex.index.store import TfidfStore, slugify
from tests.fixtures import make_repo

CODE_REPO = {
    "README.md": "# Shop API\n\nA small order service.\n",
    "src/auth.py": "def login(user, password):\n    # validate credentials against the user store\n    return check_password(user, password)\n",
    "src/orders.py": "def create_order(cart):\n    # build an order from the shopping cart and save it\n    return save(cart)\n",
    "package.json": '{"name":"shop","scripts":{"start":"node ."}}',
}


def test_chunker_emits_metadata():
    repo = make_repo(CODE_REPO)
    try:
        chunks = list(iter_chunks(Path(repo), chunk_chars=1200, overlap=100, max_files=50, max_bytes=60000))
        paths = {c["metadata"]["path"] for c in chunks}
        assert "src/auth.py" in paths and "src/orders.py" in paths
        assert all("line_start" in c["metadata"] for c in chunks)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_store_index_and_search():
    s = TfidfStore(get_settings())
    chunks = [
        {"id": "a#0", "text": "def login(user, password): validate credentials", "metadata": {"path": "src/auth.py", "line_start": 1, "line_end": 3, "language": "Python"}},
        {"id": "b#0", "text": "def create_order(cart): save order from shopping cart", "metadata": {"path": "src/orders.py", "line_start": 1, "line_end": 3, "language": "Python"}},
    ]
    s.index("nstest", chunks, {"repo_path": "/x"})
    assert s.exists("nstest")
    hits = s.search("nstest", "how do users log in with a password", k=2)
    assert hits and hits[0]["metadata"]["path"] == "src/auth.py"


def test_slugify():
    assert slugify("My Repo!") == "my-repo"


def test_chunker_aligns_to_function_boundaries():
    body = (
        "import os\n\n"
        "def alpha():\n" + "    a = 1\n" * 6 + "    return a\n\n"
        "def beta():\n" + "    b = 2\n" * 6 + "    return b\n\n"
        "def gamma():\n" + "    c = 3\n" * 6 + "    return c\n"
    )
    repo = make_repo({"src/funcs.py": body})
    try:
        chunks = list(iter_chunks(Path(repo), chunk_chars=120, overlap=30, max_files=10, max_bytes=60000))
        ours = [c for c in chunks if c["metadata"]["path"] == "src/funcs.py"]
        assert len(ours) >= 3
        # every chunk after the first opens at a def boundary, not mid-function
        for c in ours[1:]:
            assert c["text"].lstrip().startswith("def "), c["text"][:40]
        # symbols anchor the chunks and line numbers point at the real def lines
        beta = next(c for c in ours if c["metadata"]["symbol"].startswith("def beta"))
        lines = body.splitlines()
        assert lines[beta["metadata"]["line_start"] - 1].startswith("def beta")
        # index_text carries the path for retrieval
        assert ours[0]["index_text"].startswith("src/funcs.py")
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_chunker_survives_monster_lines():
    repo = make_repo({"data/blob.json": '{"k":"' + "x" * 10000 + '"}'})
    try:
        chunks = list(iter_chunks(Path(repo), chunk_chars=1000, overlap=150, max_files=10, max_bytes=60000))
        assert chunks  # terminated, produced bounded chunks
        assert all(len(c["text"]) <= 2000 for c in chunks)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_rrf_merge_prefers_agreement():
    from cortex.index.hybrid_store import rrf_merge

    a = [{"id": "x", "score": 0.9, "text": "", "metadata": {}},
         {"id": "y", "score": 0.5, "text": "", "metadata": {}}]
    b = [{"id": "z", "score": 0.8, "text": "", "metadata": {}},
         {"id": "y", "score": 0.7, "text": "", "metadata": {}}]
    merged = rrf_merge([a, b], k=3)
    # y appears in both lists -> outranks the single-list winners
    assert merged[0]["id"] == "y"
    assert {m["id"] for m in merged} == {"x", "y", "z"}


def test_spec_files_rank_below_implementation():
    from cortex.index.store import rerank

    results = [
        {"id": "spec", "score": 0.6, "text": "", "metadata": {"path": "src/app/auth/authentication.service.spec.ts"}},
        {"id": "impl", "score": 0.5, "text": "", "metadata": {"path": "src/app/auth/authentication.service.ts"}},
        {"id": "tests-dir", "score": 0.45, "text": "", "metadata": {"path": "tests/test_auth.py"}},
    ]
    out = rerank([dict(r) for r in results], "how is authentication handled", k=3)
    assert out[0]["id"] == "impl"  # spec penalized below the implementation
    assert out[1]["id"] == "spec"
    # explicit test questions: no penalty, so the spec stays above the impl
    out = rerank([dict(r) for r in results], "where are the auth tests", k=3)
    ids = [r["id"] for r in out]
    assert ids.index("spec") < ids.index("impl")


def test_self_folder_excluded_when_nested(tmp_path):
    """When Cortex's own folder sits inside the target repo, it must not be
    indexed — its prompts/evals contain users' exact question phrases."""
    from cortex.index import chunker

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main(): pass\n", encoding="utf-8")
    (tmp_path / "tool").mkdir()
    (tmp_path / "tool" / "prompts.py").write_text("What does this project do?\n", encoding="utf-8")
    original = chunker._SELF_ROOT
    chunker._SELF_ROOT = (tmp_path / "tool").resolve()
    try:
        paths = {c["metadata"]["path"] for c in
                 chunker.iter_chunks(tmp_path, chunk_chars=1200, overlap=100, max_files=50, max_bytes=60000)}
    finally:
        chunker._SELF_ROOT = original
    assert "src/app.py" in paths
    assert not any(p.startswith("tool/") for p in paths)


def test_filename_match_boosts_ranking():
    from cortex.index.store import rerank

    results = [
        {"id": "i18n", "score": 0.12, "text": "", "metadata": {"path": "src/assets/i18n/en.json"}},
        {"id": "svc", "score": 0.10, "text": "", "metadata": {"path": "src/app/auth/authentication.service.ts"}},
    ]
    out = rerank([dict(r) for r in results], "how is authentication handled?", k=2)
    assert out[0]["id"] == "svc"  # filename matches "authentication" -> boosted past noise
    # no meaningful term overlap -> original order kept
    out = rerank([dict(r) for r in results], "where is the configuration?", k=2)
    assert out[0]["id"] == "i18n"


def test_tool_copies_excluded_by_marker(tmp_path):
    """ANY copy of the tool nested in the target repo is skipped — detected by
    its package marker, not just this install's path."""
    from cortex.index import chunker

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main(): pass\n", encoding="utf-8")
    old = tmp_path / "old-copy" / "onboarding_brain"     # older package name
    old.mkdir(parents=True)
    (old / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "old-copy" / "prompts.py").write_text("What does this project do?\n", encoding="utf-8")
    new = tmp_path / "new-copy" / "cortex" / "agents"      # current layout
    new.mkdir(parents=True)
    (new / "loop.py").write_text("", encoding="utf-8")
    (tmp_path / "new-copy" / "notes.md").write_text("What does this project do?\n", encoding="utf-8")
    paths = {c["metadata"]["path"] for c in
             chunker.iter_chunks(tmp_path, chunk_chars=1200, overlap=100, max_files=50, max_bytes=60000)}
    assert "src/app.py" in paths
    assert not any(p.startswith(("old-copy/", "new-copy/")) for p in paths)


def test_dense_index_is_incremental(monkeypatch, tmp_path):
    """Re-indexing reuses vectors of unchanged chunks — only changed text is
    re-embedded (this is what makes hybrid rebuilds fast)."""
    import numpy as np

    from cortex.config import Settings
    from cortex.index import dense_store

    calls = []

    class FakeEmbedder:
        def embed(self, texts):
            calls.append(len(texts))
            return [np.full(8, float(len(t) + 1), dtype=np.float32) for t in texts]

    monkeypatch.setattr(dense_store, "_embedder", lambda name: FakeEmbedder())
    s = dense_store.DenseStore(Settings(index_dir=str(tmp_path)))
    chunks = [
        {"id": "a#0", "text": "alpha code", "metadata": {"path": "a.py"}},
        {"id": "b#0", "text": "beta code", "metadata": {"path": "b.py"}},
    ]
    s.index("inc", chunks, {})
    assert sum(calls) == 2
    # unchanged -> zero new embeddings
    calls.clear()
    s.index("inc", chunks, {})
    assert sum(calls) == 0
    # one chunk changed -> exactly one embedding
    calls.clear()
    chunks[1]["text"] = "beta code v2"
    s.index("inc", chunks, {})
    assert sum(calls) == 1


def test_chat_store_defaults_to_json(tmp_path):
    from cortex.config import Settings
    from cortex.storage.chat_history import JsonChatStore, get_chat_store

    s = get_chat_store(Settings(index_dir=str(tmp_path)))
    assert isinstance(s, JsonChatStore)


def test_chat_store_degrades_to_json_when_mongo_unreachable(tmp_path):
    from cortex.config import Settings
    from cortex.storage.chat_history import JsonChatStore, get_chat_store

    # a URI that cannot connect (or pymongo absent) must fall back, never raise
    s = get_chat_store(Settings(index_dir=str(tmp_path),
                                chat_store="mongo",
                                mongo_uri="mongodb://127.0.0.1:1/"))
    assert isinstance(s, JsonChatStore)
    s.append("nsx", "q1", "a1")
    turns = s.load("nsx")
    assert [t["role"] for t in turns] == ["user", "assistant"]
    s.clear("nsx")
    assert s.load("nsx") == []


class _FakeMongoCol:
    """Minimal in-memory stand-in honoring the $push/$each/$slice we rely on."""

    def __init__(self):
        self.docs: dict = {}

    def find_one(self, flt, proj=None):
        return self.docs.get(flt["_id"])

    def update_one(self, flt, update, upsert=False):
        doc = self.docs.setdefault(flt["_id"], {"_id": flt["_id"], "turns": []})
        push = update["$push"]["turns"]
        doc["turns"].extend(push["$each"])
        sl = push.get("$slice")
        if isinstance(sl, int) and sl < 0:
            doc["turns"] = doc["turns"][sl:]

    def delete_one(self, flt):
        self.docs.pop(flt["_id"], None)


def test_chat_store_persists_wiring_on_assistant_turn(tmp_path):
    from cortex.config import Settings
    from cortex.storage.chat_history import JsonChatStore

    s = JsonChatStore(Settings(index_dir=str(tmp_path)))
    wiring = {"nodes": [{"id": "a", "label": "a.ts"}, {"id": "b", "label": "b.ts"}],
              "edges": [{"from": "a", "to": "b", "kind": "import"}]}
    s.append("nsw", "how do files connect?", "A imports B.", extra={"wiring": wiring})
    turns = s.load("nsw")
    assert turns[0]["role"] == "user"  # user turn carries no diagram
    assert "wiring" not in turns[0]
    assert turns[1]["role"] == "assistant"
    assert turns[1]["wiring"]["edges"][0]["kind"] == "import"  # diagram survives reload


def test_mongo_chat_store_caps_and_roundtrips():
    from cortex.storage.chat_history import _HISTORY_CAP, MongoChatStore

    store = MongoChatStore(_FakeMongoCol())
    for i in range(50):  # 50 calls * 2 turns = 100, must cap to last _HISTORY_CAP
        store.append("ns", f"q{i}", f"a{i}")
    turns = store.load("ns")
    assert len(turns) == _HISTORY_CAP  # 80
    assert turns[-1] == {"role": "assistant", "content": "a49", "ts": turns[-1]["ts"]}
    assert turns[0]["role"] == "user" and turns[0]["content"] == "q10"  # oldest 20 dropped
    store.clear("ns")
    assert store.load("ns") == []


def test_enrich_folds_i18n_labels():
    # i18n labels are folded into index_text; commit subjects are intentionally
    # NOT (that broke dense-embed reuse on re-sync — history lives in its own chunks)
    from cortex.index.enrich import enrich_chunks

    chunks = [
        {"id": "a#0", "text": "title = translate('RANK_BY_STORE')",
         "index_text": "src/rank.ts\ntitle = translate('RANK_BY_STORE')",
         "metadata": {"path": "src/rank.ts"}},
    ]
    labels = {"RANK_BY_STORE": "Rank by Store"}
    n = enrich_chunks(chunks, labels)
    assert n == 1
    it = chunks[0]["index_text"]
    assert "Rank by Store" in it          # UI label folded in
    assert chunks[0]["text"] == "title = translate('RANK_BY_STORE')"  # display text untouched


def test_commit_chunks_use_stable_hash_keys():
    # commit chunks must be keyed by hash (stable across re-sync), not position
    import subprocess
    from cortex.index.enrich import build_commit_chunks

    repo = make_repo({"a.py": "x = 1\n"})
    try:
        for cmd in (["init"], ["add", "-A"], ["-c", "user.email=t@t.co", "-c", "user.name=t",
                    "commit", "-m", "first commit"]):
            subprocess.run(["git", "-C", str(repo), *cmd], capture_output=True)
        chunks = build_commit_chunks(repo, max_commits=10)
        if chunks:  # git may be unavailable in some envs
            import re as _re
            suffix = chunks[0]["id"].split("#", 1)[1]
            # a git short hash (>=6 hex chars), not a 0-based positional index
            assert _re.fullmatch(r"[0-9a-f]{6,}", suffix)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_i18n_label_builder(tmp_path):
    from cortex.index.enrich import build_i18n_labels

    (tmp_path / "src" / "assets" / "i18n").mkdir(parents=True)
    (tmp_path / "src" / "assets" / "i18n" / "en.json").write_text(
        '{"RANK_BY_STORE": "Rank by Store", "nested": {"LASSO": "Lasso Selection"}}', encoding="utf-8")
    labels = build_i18n_labels(tmp_path)
    assert labels.get("RANK_BY_STORE") == "Rank by Store"
    assert labels.get("LASSO") == "Lasso Selection"


def test_wiring_builds_import_graph(tmp_path):
    from cortex.config import Settings
    from cortex.index.store import get_store
    from cortex.graph.wiring import build_wiring

    s = Settings(index_dir=str(tmp_path))
    chunks = [
        {"id": "c#0", "text": "import { OrderService } from './order.service';\nexport class CartComponent {}",
         "metadata": {"path": "src/cart.component.ts", "line_start": 1, "line_end": 2}},
        {"id": "s#0", "text": "export class OrderService { save() {} }",
         "metadata": {"path": "src/order.service.ts", "line_start": 1, "line_end": 1}},
    ]
    get_store(s).index("wiretest", chunks, {})
    w = build_wiring("wiretest", ["src/cart.component.ts", "src/order.service.ts"], settings=s)
    assert w and len(w["nodes"]) == 2
    roles = {n["label"]: n["role"] for n in w["nodes"]}
    assert roles["cart.component.ts"] == "component" and roles["order.service.ts"] == "service"
    imp = [e for e in w["edges"] if e["kind"] == "import"]
    assert {(e["from"], e["to"]) for e in imp} >= {("src/cart.component.ts", "src/order.service.ts")}
    # single file -> nothing to draw
    assert build_wiring("wiretest", ["src/cart.component.ts"], settings=s) is None

    # unrelated files (no imports, different folders) still get connected
    chunks2 = [
        {"id": "a#0", "text": "export class A {}", "metadata": {"path": "src/x/a.ts", "line_start": 1, "line_end": 1}},
        {"id": "b#0", "text": "export class B {}", "metadata": {"path": "src/y/b.ts", "line_start": 1, "line_end": 1}},
    ]
    get_store(s).index("wiretest2", chunks2, {})
    w2 = build_wiring("wiretest2", ["src/x/a.ts", "src/y/b.ts"], settings=s)
    assert w2 and len(w2["edges"]) >= 1  # hub fallback connects them


def test_ns_dir_rejects_path_traversal():
    import pytest
    s = TfidfStore(get_settings())
    for bad in ("..", ".", "", "a/b", "a\\b", "../../etc"):
        with pytest.raises(ValueError):
            s.ns_dir(bad)


def test_ingest_blocked_outside_allowed_roots(tmp_path):
    import pytest
    from cortex.config import Settings
    from cortex.index.store import get_store
    from cortex.index.access import RepoAccessError

    repo = make_repo(CODE_REPO)
    try:
        s = Settings(allowed_roots=(str(tmp_path / "only-here"),))
        with pytest.raises(RepoAccessError):
            ingest_repo(IngestRequest(repo_path=str(repo), namespace="forbiddentest", rebuild=True), settings=s)
        # the check fired before indexing — nothing was persisted
        assert not get_store(s).exists("forbiddentest")
    finally:
        shutil.rmtree(repo, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Regression tests for the peak-accuracy audit fixes
# --------------------------------------------------------------------------- #
def test_namespace_from_clone_url():
    from cortex.index.ingest import _namespace_from_clone_url

    assert _namespace_from_clone_url("https://github.com/org/my-repo.git") == "my-repo"
    assert _namespace_from_clone_url("git@github.com:org/my-repo.git") == "my-repo"
    assert _namespace_from_clone_url("https://host/team/sub/some.repo/") == "some.repo"
    assert _namespace_from_clone_url("https://dev.azure.com/org/proj/_git/Repo") == "Repo"
    assert _namespace_from_clone_url("") == ""


def test_resync_unindexed_raises():
    import pytest

    from cortex.index.ingest import resync_namespace

    with pytest.raises(ValueError):
        resync_namespace("never-indexed-namespace")


def test_already_indexed_clone_is_not_recloned():
    # re-ingesting an already-indexed namespace must serve from cache WITHOUT
    # cloning — a bogus clone URL would error if a clone were attempted
    repo = make_repo(CODE_REPO)
    try:
        ingest_repo(IngestRequest(repo_path=str(repo), namespace="dedup", rebuild=True))
        resp = ingest_repo(IngestRequest(clone_url="https://example.invalid/x.git", namespace="dedup"))
        assert resp.already_indexed is True
        assert resp.chunks_indexed == 0
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_resync_local_repo():
    # a locally-ingested repo re-syncs from its path with no token needed
    from cortex.index.ingest import resync_namespace

    repo = make_repo(CODE_REPO)
    try:
        ingest_repo(IngestRequest(repo_path=str(repo), namespace="resynctest", rebuild=True))
        res = resync_namespace("resynctest")
        assert res["mode"] == "local"
        assert res["status"] in ("resyncing", "already_resyncing")
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_full_file_reconstructs_whole_file():
    # the file viewer needs the WHOLE file, stitched back from its chunks
    repo = make_repo({"README.md": "# X\n\nApp.\n",
                      "src/big.py": "\n".join("def f%d():\n    return %d" % (i, i) for i in range(30))})
    from cortex.index.store import get_store
    try:
        ingest_repo(IngestRequest(repo_path=str(repo), namespace="fftest", rebuild=True))
        store = get_store(get_settings())
        f = store.full_file("fftest", "src/big.py")
        assert f and "def f0(" in f["content"] and "def f29(" in f["content"]  # first AND last
        assert store.full_file("fftest", "does/not/exist.py") is None
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_ingest_accepts_quoted_windows_path():
    # Explorer's "Copy as path" wraps the path in quotes; that must not 400
    repo = make_repo(CODE_REPO)
    try:
        resp = ingest_repo(IngestRequest(repo_path=f'  "{repo}"  ', namespace="quoted", rebuild=True))
        assert resp.files_indexed > 0
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_ingest_empty_folder_explains_why(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="is empty"):
        ingest_repo(IngestRequest(repo_path=str(tmp_path), namespace="emptyx", rebuild=True))
    # a folder that only wraps a Cortex copy points the user at the inner folder
    inner = tmp_path / "Cortex-main" / "cortex" / "agents"
    inner.mkdir(parents=True)
    (inner / "loop.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="use the inner folder"):
        ingest_repo(IngestRequest(repo_path=str(tmp_path), namespace="wrapx", rebuild=True))


def test_annotations_save_update_and_clear(tmp_path):
    from cortex.config import Settings
    from cortex.storage import annotations

    s = Settings(index_dir=str(tmp_path))
    annotations.save_annotation("anntest", "src/XfrmSvc.ts", "Converts legacy orders.",
                                symbol="class XfrmSvc", settings=s)
    annotations.save_annotation("anntest", "src/XfrmSvc.ts", "Reconciles nightly billing exports.",
                                symbol="class XfrmSvc", settings=s)  # same file+symbol: replaced
    anns = annotations.load_annotations("anntest", settings=s)
    assert [a["answer"] for a in anns] == ["Reconciles nightly billing exports."]
    annotations.save_annotation("anntest", "src/XfrmSvc.ts", "", symbol="class XfrmSvc", settings=s)
    assert annotations.load_annotations("anntest", settings=s) == []  # empty answer removes it
