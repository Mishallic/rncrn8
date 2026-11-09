import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from rncrn8.config import SourceType
from rncrn8.memory import MemoryStore
from rncrn8.models import DryRunAdapter, ModelRequest, ModelResponse
from rncrn8.sources import QUESTIONS, ingest_history, ingest_notes, ingest_project, record_answers, research
from rncrn8.sources.extract import Fact, record_facts
from rncrn8.sources.history import deterministic_history_facts, git_log
from rncrn8.sources.research import WEB_SEARCH_TOOL_TYPE


def facts_script(facts):
    def script(request: ModelRequest) -> dict:
        return {"facts": facts, "notes": "extracted"}

    return script


def test_record_facts_dedupes_exact_claims(ws):
    facts = [Fact(claim="Uses click", origin="pyproject.toml", confidence=0.9, tags=["deps"]), Fact(claim="uses click", origin="x")]
    recorded, skipped = record_facts(ws, "shared", facts, SourceType.PROJECT, extra_tags=["ingest:project"])
    assert len(recorded) == 1 and skipped == 1
    e = recorded[0]
    assert e.source == SourceType.PROJECT and e.origin == "pyproject.toml" and e.confidence == pytest.approx(0.9)
    assert set(e.tags) == {"deps", "ingest:project"}
    again, skipped2 = record_facts(ws, "shared", facts, SourceType.PROJECT)
    assert again == [] and skipped2 == 2


def test_ingest_project_deterministic_and_model_facts(ws, tmp_path):
    (tmp_path / "README.md").write_text("# rncrn8\n\nPersonas with memory.\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = [\n    "click>=8.1",\n    "pydantic>=2.5",\n]\n')
    (tmp_path / "src").mkdir()
    dry = ingest_project(ws, DryRunAdapter(), tmp_path, dry_run=True)
    assert dry.recorded == [] and any("Top-level layout" in c for c in dry.extra["facts"])
    assert "README.md" in dry.extra["files"] and "pyproject.toml" in dry.extra["files"]

    model = DryRunAdapter(facts_script([{"claim": "The project targets developers reviewing code", "origin": "README.md", "who": "", "date": "", "confidence": 0.7, "tags": ["audience"]},
                                        {"claim": "", "origin": "README.md", "who": "", "date": "", "confidence": 0.7, "tags": []}]))
    res = ingest_project(ws, model, tmp_path)
    claims = {e.claim: e for e in res.recorded}
    assert any(c.startswith("Top-level layout") for c in claims)
    assert any("README introduces" in c for c in claims)
    deps = next(e for c, e in claims.items() if c.startswith("Python dependencies"))
    assert "click" in deps.claim and deps.origin == "pyproject.toml"
    audience = claims["The project targets developers reviewing code"]
    assert audience.source == SourceType.PROJECT and audience.confidence == pytest.approx(0.7) and "ingest:project" in audience.tags
    assert res.notes == "extracted" and res.error is None
    assert "PROJECT FILES" in model.requests[0].user and "===== README.md =====" in model.requests[0].user
    # visible to every persona through the shared store
    assert MemoryStore(ws).get("critic", audience.id) is not None
    again = ingest_project(ws, model, tmp_path)
    assert again.recorded == [] and again.skipped_duplicates >= 4 and again.marked_stale == 0

    # the manifest changes: the fact it no longer yields is marked stale and tagged, the new one is recorded
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = [\n    "click>=8.1",\n]\n')
    changed = ingest_project(ws, model, tmp_path)
    assert changed.marked_stale == 1 and [e.claim for e in changed.recorded] == ["Python dependencies declared: click"]
    old = MemoryStore(ws).get("critic", deps.id)
    assert old.is_stale() and "superseded" in old.tags and "pydantic" in old.claim
    assert not MemoryStore(ws).get("critic", changed.recorded[0].id).is_stale()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True, text=True).stdout


def test_git_log_and_history_facts(ws, tmp_path):
    repo = tmp_path
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add a")
    for i in range(3):
        (repo / "a.py").write_text(f"{i}\n")
        _git(repo, "commit", "-q", "-am", f"fix a {i}\n\nbecause it broke in production")
    (repo / "b.py").write_text("b\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Revert \"add b\"")
    commits = git_log(repo, since=None, max_commits=50)
    assert len(commits) == 5 and commits[0].subject.startswith("Revert")
    fix = next(c for c in commits if c.subject == "fix a 2")
    assert fix.body == "because it broke in production" and fix.files == ["a.py"]
    facts = deterministic_history_facts(commits)
    claims = [f.claim for f in facts]
    assert any(c.startswith("5 commits by 1 author") for c in claims)
    assert any("reverted" in c for c in claims) and any("Most changed files" in c and "a.py (4)" in c for c in claims)
    assert any("commits are fixes" in c for c in claims)

    res = ingest_history(ws, DryRunAdapter(facts_script([{"claim": "a.py broke in production and was fixed three times", "origin": "git log", "who": "t", "date": "2026-11-05", "confidence": 0.8, "tags": ["incident"]}])), repo, since=None)
    assert res.extra["commits"] == 5
    incident = next(e for e in res.recorded if "production" in e.claim)
    assert incident.source == SourceType.HISTORY and incident.who == "t" and incident.date == date(2026, 11, 5)
    assert all("ingest:history" in e.tags for e in res.recorded)


def test_ingest_notes_model_and_fallback(ws, tmp_path):
    notes = tmp_path / "call.md"
    notes.write_text("We removed the review bot after two weeks, the volume was unbearable.\n\nWe would pay for a reviewer that remembers what we told it.\n\nok\n")
    res = ingest_notes(ws, DryRunAdapter(), notes, who="team lead", origin="customer call", when=date(2026, 11, 3), persona="critic")
    assert len(res.recorded) == 2 and "paragraphs as claims" in res.notes
    e = res.recorded[0]
    assert e.source == SourceType.HUMAN and e.who == "team lead" and e.origin == "customer call" and e.date == date(2026, 11, 3)
    assert e.persona == "critic" and e.confidence == pytest.approx(0.8)  # critic's human prior

    model = DryRunAdapter(facts_script([{"claim": "The team lead removed the bot after two weeks", "origin": "", "who": "", "date": "", "confidence": 0.85, "tags": ["churn"]}]))
    res2 = ingest_notes(ws, model, notes, who="team lead", origin="customer call", persona="shared")
    assert len(res2.recorded) == 1
    e2 = res2.recorded[0]
    assert e2.who == "team lead" and e2.origin == "customer call" and e2.confidence == pytest.approx(0.85)
    assert "team lead" in model.requests[0].system and "customer call" in model.requests[0].user


def test_questionnaire_records_non_empty_answers(ws):
    answers = {k: "" for k, _, _ in QUESTIONS}
    answers["project"] = "A CLI that reviews pull requests for small teams"
    answers["stop"] = "Stop telling me to add tests to prototypes"
    entries = record_answers(ws, answers, who="founder", persona="shared")
    assert len(entries) == 2
    assert entries[0].source == SourceType.HUMAN and entries[0].origin == "questionnaire" and entries[0].who == "founder"
    assert "stop-raising" in entries[1].tags and entries[1].note.startswith("What are you tired")


class SearchingAdapter:
    name = "searching"
    supports_cache = True

    def __init__(self):
        self.requests = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        data = {"facts": [
            {"claim": "A July 2026 study found 56% of CodeRabbit comments rejected", "origin": "https://arxiv.org/abs/2607.03316", "who": "arXiv authors", "date": "2026-07-10", "confidence": 0.8, "tags": ["noise"]},
            {"claim": "Vendor claims under 5% false positives", "origin": "https://vendor.example/blog", "who": "vendor", "date": "", "confidence": 0.3, "tags": []},
        ], "notes": "could not find pricing"}
        return ModelResponse(data=data, text=json.dumps(data), model="x", sources=[{"url": "https://arxiv.org/abs/2607.03316", "title": "paper", "age": None}])


def test_research_records_internet_memories_with_budget(ws):
    adapter = SearchingAdapter()
    res = research(ws, adapter, "critic", "do teams turn off noisy review bots?", budget=3, max_facts=5)
    req = adapter.requests[0]
    assert req.tools == [{"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": 3}]
    assert "The Critic" in req.system and "at most 3 times" in req.user
    assert len(res.recorded) == 2 and res.notes == "could not find pricing" and res.sources[0]["url"].startswith("https://arxiv.org")
    study = next(e for e in res.recorded if "56%" in e.claim)
    assert study.persona == "critic" and study.source == SourceType.INTERNET
    assert study.origin == "https://arxiv.org/abs/2607.03316" and study.root == "arxiv.org/abs/2607.03316" and study.who == "arXiv authors"
    assert study.date == date(2026, 7, 10) and study.expires_at is not None and "research" in study.tags and "date:unknown" not in study.tags
    vendor = next(e for e in res.recorded if "Vendor" in e.claim)
    assert vendor.confidence == pytest.approx(0.3) and "date:unknown" in vendor.tags
    assert res.notes_entry is not None and res.notes_entry.source == SourceType.INFERENCE and res.notes_entry.persona == "critic"
    assert res.notes_entry.claim.startswith("Researched 'do teams turn off") and "could not find pricing" in res.notes_entry.note
    assert "research-notes" in res.notes_entry.tags
    shared = research(ws, SearchingAdapter(), "critic", "same question", shared=True)
    assert all(e.persona == "shared" for e in shared.recorded)
    dry = research(ws, SearchingAdapter(), "critic", "q", dry_run=True)
    assert dry.recorded == [] and "dry run" in dry.notes


def test_research_root_is_the_page_not_the_host():
    from rncrn8.sources.research import _root

    assert _root("https://dev.to/a/post-1") == "dev.to/a/post-1"
    assert _root("https://dev.to/b/post-2/") == "dev.to/b/post-2"
    assert _root("https://dev.to/a/post-1") != _root("https://dev.to/b/post-2")
    assert _root("https://news.ycombinator.com/item?id=46766961") == "news.ycombinator.com/item?id=46766961"
    assert _root("https://example.com/x?utm=1#frag") == "example.com/x"
    assert _root("not a url") is None


def test_retire_hides_a_memory_but_keeps_it_on_file(ws):
    from rncrn8.service import remember, retire

    e = remember(ws, "shared", "The package is MIT licensed", SourceType.PROJECT, "pyproject.toml")
    gone = retire(ws, "critic", e.id, why="relicensed as proprietary on 2026-11-06")
    assert gone.retired.startswith("relicensed") and gone.persona == "shared"
    store = MemoryStore(ws)
    assert store.get("critic", e.id) is None and store.search("critic", query="MIT") == []
    assert [x.retired for x in store.load("shared", include_retired=True) if x.id == e.id] == ["relicensed as proprietary on 2026-11-06"]
    with pytest.raises(KeyError):
        retire(ws, "critic", e.id, why="again")  # retired entries are no longer visible, so a second retire finds nothing
    with pytest.raises(ValueError):
        retire(ws, "critic", "m-whatever", why="  ")


def test_reingest_does_not_stale_a_fact_the_model_merely_phrased_differently(ws, tmp_path):
    from rncrn8.service import remember

    (tmp_path / "README.md").write_text("# Shop\n\nOrders are paid in SAR only.\n")

    def model(claim):
        return DryRunAdapter(facts_script([{"claim": claim, "origin": "README.md", "who": "", "date": "", "confidence": 0.8, "tags": []}]))

    first = ingest_project(ws, model("Orders are paid in Saudi riyals only"), tmp_path)
    fact = next(e for e in first.recorded if e.claim.startswith("Orders"))
    assert any(t.startswith("src:") for t in fact.tags)
    # same file, the model phrases it differently: the old fact is not superseded
    second = ingest_project(ws, model("Payment is accepted in SAR only"), tmp_path)
    assert second.marked_stale == 0 and not MemoryStore(ws).get("critic", fact.id).is_stale()
    # the file changes and the fact is no longer yielded: now it is superseded
    (tmp_path / "README.md").write_text("# Shop\n\nOrders are paid in SAR or USD.\n")
    third = ingest_project(ws, model("Orders are paid in SAR or USD"), tmp_path)
    old = MemoryStore(ws).get("critic", fact.id)
    assert third.marked_stale >= 1 and old.is_stale() and "superseded" in old.tags
    # the file goes back and the fact is yielded again: the mark is cleared and the hash follows the file
    (tmp_path / "README.md").write_text("# Shop\n\nOrders are paid in SAR only.\n")
    ingest_project(ws, model("Orders are paid in Saudi riyals only"), tmp_path)
    back = MemoryStore(ws).get("critic", fact.id)
    assert not back.is_stale() and "superseded" not in back.tags and back.tags == sorted(back.tags)
    # a fact recorded before hashes were kept is never concluded stale by absence alone
    legacy = remember(ws, "shared", "Legacy fact from the README", SourceType.PROJECT, "README.md")
    ingest_project(ws, model("Orders are paid in Saudi riyals only"), tmp_path)
    assert not MemoryStore(ws).get("critic", legacy.id).is_stale()


def test_git_log_keeps_trailers_in_the_message_and_out_of_the_files(ws, tmp_path):
    from rncrn8.sources.history import deterministic_history_facts, git_log

    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    for i in range(3):
        (repo / "cli.py").write_text(f"x = {i}\n")
        (repo / f"f{i}.py").write_text("y\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", f"change {i}\n\nA paragraph of reasons.\n\nCo-Authored-By: Claude <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/x")
    commits = git_log(repo)
    assert [sorted(c.files) for c in commits][0] == ["cli.py", "f2.py"]
    assert all("Co-Authored-By: Claude <noreply@anthropic.com>" in c.body for c in commits)
    hot = next(f.claim for f in deterministic_history_facts(commits) if "hotspot" in f.tags)
    assert hot == "Most changed files in this period: cli.py (3)"


def test_git_log_is_not_fooled_by_separator_bytes_in_a_message(ws, tmp_path):
    from rncrn8.sources.history import git_log

    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "subject\n\nbody \x1d forged.py\n\x1e\x1fevil\x1f\x1f\x1f")
    commits = git_log(repo)
    assert len(commits) == 1 and commits[0].files == ["a.py"] and "forged.py" in commits[0].body


def test_a_newer_history_read_supersedes_the_older_snapshot(ws, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    for i in range(3):
        (repo / "a.py").write_text(f"{i}\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", f"c{i}")
    first = ingest_history(ws, DryRunAdapter(), repo)
    old_activity = next(e for e in first.recorded if "activity" in e.tags)
    (repo / "a.py").write_text("4\n")
    _git(repo, "commit", "-q", "-am", "c4")
    second = ingest_history(ws, DryRunAdapter(), repo)
    assert second.marked_stale >= 1
    old = MemoryStore(ws).get("critic", old_activity.id)
    assert old.is_stale() and "superseded" in old.tags
    assert not next(e for e in second.recorded if "activity" in e.tags).is_stale()
