import subprocess
from pathlib import Path

import pytest

from rncrn8.adapters import git_diff_packet
from rncrn8.memory import MemoryStore, Workspace
from rncrn8.models import DryRunAdapter, ModelRequest
from rncrn8.persona import scaffold_persona
from rncrn8.review import run_review
from rncrn8.followup import followup


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def code_ws(tmp_path: Path):
    repo = tmp_path
    _git(repo, "init", "-q")
    (repo / "app.py").write_text("\n".join(f"line {i}" for i in range(1, 41)) + "\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    lines = (repo / "app.py").read_text().splitlines()
    lines[9] = "line 10 changed"
    lines[29] = "line 30 changed"
    (repo / "app.py").write_text("\n".join(lines) + "\n")
    _git(repo, "commit", "-q", "-am", "change")
    ws = Workspace.init(repo)
    scaffold_persona(ws, "security", template="security")
    return ws, repo


def _script(request: ModelRequest) -> dict:
    return {"understood": "", "notes": "", "prior_findings": [], "findings": [
        {"kind": "problem", "claim": "line 10 is unsafe", "where": "app.py:10", "rationale": "", "fix": "", "confidence": 0.8, "supports": [], "opposes": []},
        {"kind": "risk", "claim": "line 30 is odd", "where": "[app.py:29-31]", "rationale": "", "fix": "", "confidence": 0.7, "supports": [], "opposes": []},
        {"kind": "problem", "claim": "no location", "where": "somewhere", "rationale": "", "fix": "", "confidence": 0.6, "supports": [], "opposes": []},
        {"kind": "strength", "claim": "line 10 reads well", "where": "app.py:10", "rationale": "", "fix": "", "confidence": 0.7, "supports": [], "opposes": []},
    ]}


def test_followup_closes_touched_findings_only(code_ws):
    ws, repo = code_ws
    packet = git_diff_packet(repo, "HEAD~1..HEAD")
    out = run_review(ws, packet, DryRunAdapter(_script), panel=["security"], summaries=False)
    rid = out.review.id
    res = followup(ws, rid)
    assert res.notes == ["nothing has been committed since the reviewed commit"] and res.items == []

    # a later commit changes line 10 only
    lines = (repo / "app.py").read_text().splitlines()
    lines[9] = "line 10 fixed"
    (repo / "app.py").write_text("\n".join(lines) + "\n")
    _git(repo, "commit", "-q", "-am", "fix 10")

    dry = followup(ws, rid, dry_run=True)
    by_claim = {i.claim: i for i in dry.items}
    assert by_claim["line 10 is unsafe"].touched and not by_claim["line 10 is unsafe"].recorded
    assert not by_claim["line 30 is odd"].touched
    assert by_claim["no location"].note.startswith("cited location is not path:line")
    assert by_claim["line 10 reads well"].note.startswith("kind 'strength' is not closable")  # same lines changed, never closed by a commit
    assert all(d.outcome is None for d in MemoryStore(ws).load_decisions())

    res = followup(ws, rid)
    by_claim = {i.claim: i for i in res.items}
    assert by_claim["line 10 is unsafe"].recorded and not by_claim["line 10 reads well"].recorded
    store = MemoryStore(ws)
    closed = [d for d in store.load_decisions() if d.outcome is not None]
    assert len(closed) == 1 and closed[0].where == "app.py:10" and closed[0].kind == "problem"
    assert closed[0].outcome is True and closed[0].outcome_source.startswith("implicit:commit:")
    assert closed[0].outcome_learning_rate == pytest.approx(ws.load_config().outcomes.commit_learning_rate)
    # second run: nothing open is touched; the closed one is not re-closed
    again = followup(ws, rid)
    assert all(not i.recorded for i in again.items) and len(again.items) == 3


def test_followup_only_closes_findings_whose_own_lines_changed(code_ws):
    ws, repo = code_ws
    packet = git_diff_packet(repo, "HEAD~1..HEAD")
    out = run_review(ws, packet, DryRunAdapter(_script), panel=["security"], summaries=False)
    # change line 12, two lines below the finding at line 10: inside the hunk's context, not the cited line
    lines = (repo / "app.py").read_text().splitlines()
    lines[11] = "line 12 changed"
    (repo / "app.py").write_text("\n".join(lines) + "\n")
    _git(repo, "commit", "-q", "-am", "nearby")
    res = followup(ws, out.review.id)
    assert not any(i.touched for i in res.items)


def test_followup_rejects_tampered_provenance(code_ws):
    import json

    ws, repo = code_ws
    packet = git_diff_packet(repo, "HEAD~1..HEAD")
    out = run_review(ws, packet, DryRunAdapter(_script), panel=["security"], summaries=False)
    pj = out.review_dir / "packet.json"
    data = json.loads(pj.read_text())
    data["metadata"]["reviewed"] = "--output=" + str(repo / "pwned")
    pj.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="committed range"):
        followup(ws, out.review.id)
    assert not (repo / "pwned").exists()
    data["metadata"]["reviewed"] = "0" * 40
    data["metadata"]["repo"] = str(repo / "nope")
    pj.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="not a git repository"):
        followup(ws, out.review.id)


def test_followup_requires_a_committed_range(code_ws):
    ws, repo = code_ws
    (repo / "app.py").write_text((repo / "app.py").read_text() + "extra\n")
    packet = git_diff_packet(repo, "HEAD")
    out = run_review(ws, packet, DryRunAdapter(_script), panel=["security"], summaries=False)
    with pytest.raises(ValueError, match="committed range"):
        followup(ws, out.review.id)
    with pytest.raises(FileNotFoundError):
        followup(ws, "r-nope")


def test_followup_reads_exact_lines_and_leaves_broad_citations_to_a_person(tmp_path):
    from rncrn8.adapters.diff import cited_ranges

    assert cited_ranges("src/a.py:1-1833") == ("src/a.py", [(1, 1833)])
    assert cited_ranges("[src/a.py:1-1833] lines 212, 1732, 1757-1758") == ("src/a.py", [(212, 212), (1732, 1732), (1757, 1758)])
    assert cited_ranges("[src/a.py:10-20] lines 15 and 99") == ("src/a.py", [(15, 15)])  # a named line outside the section is ignored
    assert cited_ranges("§2") is None

    repo = tmp_path
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "big.py").write_text("\n".join(f"line {i}" for i in range(1, 301)) + "\n")  # a new file: one 300-line hunk
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add big")
    ws = Workspace.init(repo)
    scaffold_persona(ws, "security", template="security")

    def script(request: ModelRequest) -> dict:
        assert "never a whole section" in request.system
        return {"understood": "", "notes": "", "prior_findings": [], "findings": [
            {"kind": "problem", "claim": "the whole file", "where": "big.py:1-300", "rationale": "", "fix": "", "confidence": 0.8, "supports": [], "opposes": []},
            {"kind": "problem", "claim": "line 40", "where": "[big.py:1-300] lines 40-41", "rationale": "", "fix": "", "confidence": 0.8, "supports": [], "opposes": []},
            {"kind": "risk", "claim": "line 200", "where": "big.py:200", "rationale": "", "fix": "", "confidence": 0.7, "supports": [], "opposes": []},
        ]}

    out = run_review(ws, git_diff_packet(repo, "HEAD~1..HEAD"), DryRunAdapter(script), panel=["security"], summaries=False)
    lines = (repo / "big.py").read_text().splitlines()
    lines[39] = "line 40 fixed"
    (repo / "big.py").write_text("\n".join(lines) + "\n")
    _git(repo, "commit", "-q", "-am", "fix 40")
    by = {i.claim: i for i in followup(ws, out.review.id).items}
    assert by["the whole file"].note.startswith("cites 300 lines, too broad") and not by["the whole file"].recorded
    assert by["line 40"].touched and by["line 40"].recorded
    assert not by["line 200"].touched and by["line 200"].note is None
