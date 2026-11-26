"""The three agents — chat, tour, walkthrough — with a scripted agent in place of
Claude (see `fake_agent` in conftest.py). These check the rules AROUND the model:
grounded sources, dropping invented paths, honest not-found, failure reporting.
"""
import shutil

import pytest

from cortex.contract import AskRequest, IngestRequest, TourResponse, WalkthroughResponse
from cortex.agents.loop import AgentFailedError
from cortex.index.ingest import ingest_repo
from tests.fixtures import make_repo

REPO = {
    "README.md": "# Pipeline\n\nA small ETL pipeline.\n",
    "etl/main.py": "from etl.extract import pull\nfrom etl.load import push\n\nif __name__ == '__main__':\n    push(pull())\n",
    "etl/extract.py": "def pull():\n    return [1, 2, 3]\n",
    "etl/load.py": "def push(rows):\n    print(rows)\n",
    "etl/unused.py": "X = 1\n",
}


@pytest.fixture
def repo_ns():
    repo = make_repo(REPO)
    ingest_repo(IngestRequest(repo_path=str(repo), namespace="agentic", rebuild=True))
    yield "agentic"
    shutil.rmtree(repo, ignore_errors=True)


# ---- chat agent -------------------------------------------------------------

def test_chat_sources_are_cited_files_the_agent_read(repo_ns, fake_agent):
    from cortex.agents.chat import ask, load_chat_history

    fake_agent.reads = ["etl/main.py", "etl/extract.py", "etl/load.py"]
    fake_agent.reply = {
        "found": True,
        "answer": "Execution starts in `etl/main.py`, which calls `pull()` from `etl/extract.py`.",
        "sources": [
            {"path": "etl/main.py", "line_start": 4, "line_end": 5},
            {"path": "extract.py", "line_start": 1, "line_end": 2},      # partial path → resolved
            {"path": "etl/unused.py", "line_start": 1, "line_end": 1},   # cited but never read
            {"path": "etl/ghost.py", "line_start": 1, "line_end": 1},    # invented
        ]}
    r = ask(AskRequest(namespace=repo_ns, question="where does the pipeline start?"))

    assert [s.path for s in r.sources] == ["etl/main.py", "etl/extract.py"]
    main = r.sources[0]
    assert (main.line_start, main.line_end) == (4, 5)          # Claude's exact line range
    assert main.snippet == "if __name__ == '__main__':\n    push(pull())"   # from the file
    assert r.grounded is True and r.validation_status == "passed"
    assert r.trace.agent_id == "kt-agent-v1" and r.trace.grounding["tool_calls"] == 3
    turns = load_chat_history(repo_ns)
    assert [t["role"] for t in turns] == ["user", "assistant"]


def test_chat_not_found_has_no_sources(repo_ns, fake_agent):
    from cortex.agents.chat import ask

    fake_agent.reads = ["etl/main.py"]
    fake_agent.reply = {"found": False, "sources": [],
                        "answer": "I couldn't find this in the indexed code. There is no payment module."}
    r = ask(AskRequest(namespace=repo_ns, question="how are payments processed?"))
    assert r.sources == [] and r.grounded is False


def test_chat_with_no_answer_says_so_clearly(repo_ns, fake_agent):
    from cortex.agents.chat import NO_ANSWER, ask

    fake_agent.reply = "ran out of turns"   # no structured output
    r = ask(AskRequest(namespace=repo_ns, question="where does it start?"))
    assert r.answer == NO_ANSWER and r.grounded is False
    assert "no_answer" in r.trace.errors and r.validation_status == "warning"


def test_chat_greeting_skips_the_agent(repo_ns, fake_agent):
    from cortex.agents.chat import ask

    r = ask(AskRequest(namespace=repo_ns, question="hi"))
    assert r.trace.strategy == "conversational"
    assert fake_agent.system_prompts == []  # Claude was never called


def test_chat_reports_missing_claude_login(repo_ns, monkeypatch):
    import cortex.agents.loop as loop
    from cortex.agents.chat import ask

    def no_login(*_a, **_k):
        raise loop.AgentUnavailableError("no subscription credentials found")

    monkeypatch.setattr(loop, "run_tool_agent", no_login)
    with pytest.raises(loop.AgentUnavailableError):  # not retried, not hidden as "not found"
        ask(AskRequest(namespace=repo_ns, question="where does it start?"))


def test_chat_unindexed_namespace_errors():
    from cortex.agents.chat import ask

    with pytest.raises(ValueError):
        ask(AskRequest(namespace="does-not-exist", question="how does authentication work?"))


# ---- tour agent -------------------------------------------------------------

def test_tour_uses_agent_structure_and_drops_ungrounded_paths(repo_ns, fake_agent):
    from cortex.agents.tour import build_tour

    fake_agent.reads = ["etl/main.py", "etl/extract.py", "etl/load.py"]
    fake_agent.reply = {
        "overview": "An ETL pipeline: extract, then load.",
        "entry_point": "etl/main.py",
        "chapters": [
            {"title": "Pipeline driver", "why": "Where a run starts.",
             "stops": [{"path": "etl/main.py", "reason": "runs the pipeline", "insight": "Calls pull then push."}]},
            {"title": "Stages", "why": "The two ETL stages.",
             "stops": [{"path": "extract.py", "reason": "stage 1", "insight": "Returns rows."},  # partial path
                       {"path": "etl/made_up.py", "reason": "x", "insight": "invented"},        # not indexed
                       {"path": "etl/unused.py", "reason": "x", "insight": "never opened"},     # not seen
                       {"path": "etl/load.py", "reason": "stage 2", "insight": "Prints rows."}]},
        ],
    }
    t = build_tour(repo_ns)
    TourResponse.model_validate(t)  # still the published contract

    assert [c["title"] for c in t["chapters"]] == ["Pipeline driver", "Stages"]
    paths = [s["path"] for c in t["chapters"] for s in c["stops"]]
    assert paths == ["etl/main.py", "etl/extract.py", "etl/load.py"]
    assert t["entry_point"] == "etl/main.py" and t["narrated"] is True
    assert t["overview"].startswith("An ETL pipeline")
    # connections come from the real import graph, not from the model
    main = t["chapters"][0]["stops"][0]
    assert {c["path"] for c in main["connections"] if c["direction"] == "uses"} == {
        "etl/extract.py", "etl/load.py"}


def test_tour_reports_failure_when_agent_returns_no_tour(repo_ns, fake_agent):
    from cortex.agents.tour import build_tour

    fake_agent.reply = "I ran out of turns."   # no structured output → {}
    with pytest.raises(AgentFailedError):
        build_tour(repo_ns)


def test_tour_reports_failure_when_every_path_is_invented(repo_ns, fake_agent):
    from cortex.agents.tour import build_tour

    fake_agent.reply = {"overview": "x", "entry_point": "ghost.py", "chapters": [
        {"title": "Ghosts", "why": "none of these exist", "stops": [
            {"path": "ghost.py", "reason": "r", "insight": "i"},
            {"path": "phantom.py", "reason": "r", "insight": "i"}]}]}
    with pytest.raises(AgentFailedError):
        build_tour(repo_ns)


# ---- walkthrough agents (planner + writers) ---------------------------------

def test_walkthrough_plans_its_own_sections(repo_ns, fake_agent):
    from cortex.agents.walkthrough import build_walkthrough

    fake_agent.reads = ["etl/main.py", "etl/extract.py"]

    def reply(system_prompt, prompt):
        if "PLANNING" in system_prompt:
            return {"stack": ["Python"], "sections": [
                {"key": "what", "title": "What this pipeline does", "goal": "Big picture",
                 "files": ["README.md", "etl/main.py"]},
                {"key": "extract", "title": "Extract stage", "goal": "How rows are pulled",
                 "files": ["etl/extract.py", "etl/ghost.py"]},
                {"key": "run", "title": "Running it", "goal": "How to run", "files": ["etl/main.py"]},
            ]}
        return {"explanation": "Execution starts in `etl/main.py`, which calls **pull** "
                               "from `etl/extract.py`.",
                "takeaways": ["main.py drives the run"],
                # load.py was never read, ghost.py doesn't exist: both must be dropped
                "files": ["etl/main.py", "etl/extract.py", "etl/load.py", "etl/ghost.py"]}

    fake_agent.reply = reply
    doc = build_walkthrough(repo_ns)
    WalkthroughResponse.model_validate(doc)

    assert [s["title"] for s in doc["sections"]] == [
        "What this pipeline does", "Extract stage", "Running it"]
    assert doc["stack"] == ["Python"] and doc["generated_with"].endswith("(agent)")
    for s in doc["sections"]:
        # only files the writer cited that are real AND were read
        assert set(s["files"]) == {"etl/main.py", "etl/extract.py"}
        assert s["takeaways"] == ["main.py drives the run"]


def test_walkthrough_reports_failure_on_bad_plan(repo_ns, fake_agent):
    from cortex.agents.walkthrough import build_walkthrough

    fake_agent.reply = {"stack": [], "sections": [  # fewer than 3 sections
        {"key": "only", "title": "Only one", "goal": "", "files": []}]}
    with pytest.raises(AgentFailedError):
        build_walkthrough(repo_ns)


def test_resolve_indexed_path_drops_invented_and_ambiguous_paths():
    from cortex.agents.loop import resolve_indexed_path

    known = {"src/app/main.py", "src/util.py", "lib/util.py"}
    assert resolve_indexed_path("./src/app/main.py", known) == "src/app/main.py"
    assert resolve_indexed_path("app/main.py", known) == "src/app/main.py"  # unique suffix
    assert resolve_indexed_path("util.py", known) is None                    # ambiguous
    assert resolve_indexed_path("nope.py", known) is None                    # invented
