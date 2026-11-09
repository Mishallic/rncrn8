import json
import re

from click.testing import CliRunner

from rncrn8.cli import main


def run(runner, *args, cwd=None):
    result = runner.invoke(main, list(args), catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result.output


def test_full_cli_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()

    out = run(r, "init")
    assert "workspace:" in out
    assert (tmp_path / ".rncrn8" / "config.yaml").exists()

    run(r, "persona", "new", "critic", "--template", "critic")
    run(r, "persona", "new", "fresh")
    out = run(r, "persona", "list")
    assert "critic" in out and "fresh" in out
    assert "The Critic" in run(r, "persona", "templates")

    a = run(r, "remember", "critic", "--claim", "forums say bots are noisy", "--source", "internet", "--origin", "https://forum", "--tag", "noise")
    b = run(r, "remember", "critic", "--claim", "team lead dropped the bot", "--source", "human", "--who", "lead", "--origin", "call", "--tag", "noise")
    c = run(r, "remember", "critic", "--claim", "vendor says accurate", "--source", "internet", "--origin", "https://vendor", "--confidence", "0.3")
    ids = [re.match(r"(m-[0-9a-f]+)", x).group(1) for x in (a, b, c)]

    recalled = json.loads(run(r, "recall", "critic", "--tag", "noise", "--json"))
    assert {e["id"] for e in recalled} == set(ids[:2])
    assert all("source" in e and "origin" in e and "reliability" in e for e in recalled)

    out = run(r, "judge", "critic", "--claim", "noise kills adoption", "--supports", f"{ids[0]},{ids[1]}", "--opposes", ids[2])
    assert "RAISE" in out
    assert "uninformative" in out  # the 0.3 vendor claim
    did = re.search(r"recorded as (d-[0-9a-f]+)", out).group(1)

    assert did in run(r, "decisions", "--open")

    out = run(r, "outcome", did, "--confirmed", "--note", "they churned", "--source", "user")
    assert "confirmed" in out and "origins learned" in out
    assert "no decisions" in run(r, "decisions", "--open")

    verbose = run(r, "recall", "critic", "-v", "--tag", "noise")
    assert "confirmed by 1" in verbose


def test_commands_fail_cleanly_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["persona", "list"])
    assert result.exit_code != 0
    assert "`rncrn8 init` in the project folder creates one" in result.output


def test_judge_unknown_memory_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    run(r, "init")
    run(r, "persona", "new", "critic", "--template", "critic")
    result = r.invoke(main, ["judge", "critic", "--claim", "x", "--supports", "m-missing"])
    assert result.exit_code != 0 and "no memory" in result.output


def test_recall_unknown_persona_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    run(r, "init")
    result = r.invoke(main, ["recall", "ghost"])
    assert result.exit_code != 0 and "no persona named 'ghost'" in result.output
    assert "nothing remembered" in run(r, "recall", "shared")


def test_review_dry_run_and_persisted_review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    run(r, "init")
    run(r, "persona", "new", "critic", "--template", "critic")
    doc = tmp_path / "plan.md"
    doc.write_text("# Plan\n\nShip it.\n")

    out = run(r, "review", "plan.md", "--dry-run")
    assert "critic: SYSTEM PROMPT" in out and "critic: USER PROMPT" in out and "[§1] Plan" in out
    assert "DRY RUN" in out
    assert "no reviews yet" in run(r, "reviews", "list")

    out = run(r, "review", "plan.md", "--adapter", "dry-run", "--kind", "idea")
    assert "saved to" in out and "## Raised (0)" in out
    listing = run(r, "reviews", "list")
    rid = re.search(r"(r-[0-9a-f]+)", listing).group(1)
    assert "idea" in listing
    assert "# Review " + rid in run(r, "reviews", "show", rid)
    assert json.loads(run(r, "reviews", "show", rid, "--json"))["kind"] == "idea"

    result = r.invoke(main, ["review", "plan.md", "--adapter", "dry-run", "--panel", "ghost"])
    assert result.exit_code != 0 and "ghost" in result.output
    result = r.invoke(main, ["reviews", "show", "r-nope"])
    assert result.exit_code != 0


def test_decisions_show_full_claims_and_filters(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    run(r, "init")
    run(r, "persona", "new", "critic", "--template", "critic")
    long_claim = "The quick start's final step cannot be run as written because the judge output never displays a decision id anywhere"
    a = run(r, "remember", "critic", "--claim", "x", "--source", "human", "--origin", "call")
    mid = re.match(r"(m-[0-9a-f]+)", a).group(1)
    out = run(r, "judge", "critic", "--claim", long_claim, "--supports", mid)
    did = re.search(r"recorded as (d-[0-9a-f]+)", out).group(1)
    listing = run(r, "decisions", "--open")
    assert long_claim in listing and did in listing and "judge" in listing
    assert json.loads(run(r, "decisions", "--json"))[0]["id"] == did
    assert "no decisions" in run(r, "decisions", "--review", "r-nope")
    run(r, "outcome", did, "--refuted", "--source", "loop")
    assert "refuted, source loop, teaches nothing" in run(r, "decisions")


def test_calibration_panel_replay_commands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    run(r, "init")
    run(r, "persona", "new", "critic", "--template", "critic")
    (tmp_path / "plan.md").write_text("# Plan\n\nShip it.\n")
    assert "no closed decisions" in run(r, "calibration")
    assert "no panel weights" in run(r, "panel")
    assert "no closed decisions" in run(r, "replay")

    from rncrn8.memory import Workspace
    from rncrn8.models import DryRunAdapter, ModelRequest
    from rncrn8.packet import load_packet
    from rncrn8.review import run_review

    ws = Workspace.find(tmp_path)

    def script(request: ModelRequest) -> dict:
        return {"understood": "", "notes": "", "prior_findings": [], "findings": [
            {"kind": "problem", "claim": "x", "where": "§1", "rationale": "", "fix": "", "confidence": 0.7, "supports": [], "opposes": []}]}

    ids = []
    for _ in range(3):
        out = run_review(ws, load_packet(tmp_path / "plan.md"), DryRunAdapter(script), panel=["critic"], summaries=False)
        ids.append((out.review.id, out.review.personas[0].findings[0].decision_id))
    out = run(r, "outcome", f"{ids[0][0]}:critic:F1", "--confirmed", "--source", "user")
    assert ids[0][1] in out and "panel:" in out and "only one has a record" in out
    run(r, "outcome", ids[1][1], "--refuted", "--source", "user")
    run(r, "outcome", ids[2][1], "--confirmed", "--source", "user")
    cal = run(r, "calibration")
    assert "critic: 3 readings with outcomes · came true 2/3" in cal and "stated -> calibrated" in cal
    assert "weight 1.00" in run(r, "panel")  # one persona: uniform
    from rncrn8.track import PanelState

    two = PanelState(weights={"security": 0.53, "critic": 0.47}, n={"security": 7, "critic": 7}, brier_sum={"security": 0.98, "critic": 1.33},
                     right={"security": 5, "critic": 6})
    two.save(ws.panel_path)
    shown = run(r, "panel")
    assert "right side 5/7 · mean brier 0.140" in shown and "weight follows mean brier, not right side" in shown
    rep = run(r, "replay")
    assert "recorded" in rep and "current" in rep
    (tmp_path / "cand.yaml").write_text("prior_belief: 0.3\n")
    rep = run(r, "replay", "--candidate", "cand.yaml")
    assert "candidate" in rep and ("candidate is better" in rep or "not comparable" in rep)
    rep = run(r, "replay", "--without-learning")
    assert "no-learning" in rep and "learning vs no learning: too few held-out decisions" in rep  # three outcomes are too few to say
    both = run(r, "replay", "--candidate", "cand.yaml", "--without-learning")
    assert "candidate" in both and "no-learning" in both and "learning vs no learning:" in both
    payload = json.loads(run(r, "replay", "--json"))
    assert payload["results"][0]["label"] == "current"
    assert "NaN" not in json.dumps(payload)  # undefined metrics are null, never the invalid bare token


def test_replay_single_class_holdout_prints_na_with_reason(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    run(r, "init")
    run(r, "persona", "new", "critic", "--template", "critic")
    (tmp_path / "plan.md").write_text("# Plan\n\nShip it.\n")
    from rncrn8.memory import Workspace
    from rncrn8.models import DryRunAdapter, ModelRequest
    from rncrn8.packet import load_packet
    from rncrn8.review import run_review

    ws = Workspace.find(tmp_path)

    def script(request: ModelRequest) -> dict:
        return {"understood": "", "notes": "", "prior_findings": [], "findings": [
            {"kind": "problem", "claim": "x", "where": "§1", "rationale": "", "fix": "", "confidence": 0.7, "supports": [], "opposes": []}]}

    for _ in range(6):
        out = run_review(ws, load_packet(tmp_path / "plan.md"), DryRunAdapter(script), panel=["critic"], summaries=False)
        run(r, "outcome", out.review.personas[0].findings[0].decision_id, "--confirmed", "--source", "user")
    rep = run(r, "replay", "--holdout", "0.34")
    assert "n/a" in rep and "same answer" in rep and "nan" not in rep.lower().replace("n/a", "")
    cal = run(r, "calibration")
    assert "auc n/a (every outcome had the same answer)" in cal


def test_packet_review_diff_text_and_followup_commands(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    g = lambda *a: subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True, text=True)
    g("init", "-q")
    (tmp_path / "app.py").write_text("a = 1\n")
    g("add", "."); g("commit", "-q", "-m", "one")
    (tmp_path / "app.py").write_text("a = 2\n")
    g("commit", "-q", "-am", "two")
    run(r, "init")
    run(r, "persona", "new", "security", "--template", "security")
    run(r, "persona", "new", "investor", "--template", "investor")

    out = run(r, "packet", "--diff", "HEAD~1..HEAD", "--json")
    pk = json.loads(out)
    assert pk["kind"] == "code" and pk["sections"][0]["ref"].startswith("app.py:")
    rendered = run(r, "packet", "--diff", "HEAD~1..HEAD")
    assert rendered.startswith("TITLE:") and "[app.py:" in rendered
    run(r, "packet", "--diff", "HEAD~1..HEAD", "--out", "d.packet.json")
    assert json.loads((tmp_path / "d.packet.json").read_text())["kind"] == "code"

    out = run(r, "review", "--diff", "HEAD~1..HEAD", "--adapter", "dry-run", "--no-summaries")
    assert "saved to" in out
    assert "- security:" in out  # the security persona took part
    rid = re.search(r"(r-[0-9a-f]+)", run(r, "reviews", "list")).group(1)
    out = run(r, "followup", rid)
    assert "nothing has been committed" in out

    out = run(r, "review", "--text", "We sell memory to AI personas.", "--adapter", "dry-run", "--no-summaries")
    assert "kind: idea" in out and "investor" in out

    result = r.invoke(main, ["review", "app.py", "--diff", "HEAD"])
    assert result.exit_code != 0 and "exactly one of" in result.output
    result = r.invoke(main, ["review", "--adapter", "dry-run"])
    assert result.exit_code != 0 and "exactly one of" in result.output
    result = r.invoke(main, ["followup", "r-nope"])
    assert result.exit_code != 0


def test_replay_reports_not_comparable_when_nothing_can_be_replayed(tmp_path, monkeypatch):
    """Closed decisions that predate raw witness fields: recorded beliefs score, replays cannot."""
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    run(r, "init")
    run(r, "persona", "new", "critic", "--template", "critic")
    from rncrn8.memory import DecisionRecord, MemoryStore, Workspace

    ws = Workspace.find(tmp_path)
    store = MemoryStore(ws)
    for i in range(6):
        old_style = DecisionRecord(
            persona="critic", claim=f"c{i}", prior=0.5, log_odds=0.8, belief=0.69, threshold=0.33, raise_finding=True, severity=1.4,
            contributions=[{"ref": "self", "supports": True, "reliability": 0.69, "weight": 0.8, "decay": 1.0, "dependence": 1.0, "stale_factor": 1.0, "contribution": 0.8, "label": "own reading", "persona": "critic"}],
            outcome=i % 2 == 0,
        )
        store.append_decision(old_style)
    (tmp_path / "cand.yaml").write_text("prior_belief: 0.3\n")
    rep = run(r, "replay", "--candidate", "cand.yaml", "--holdout", "0.34")
    assert "none of the 2 held-out decision(s) stored raw witness fields" in rep
    assert "not comparable" in rep and "candidate is better" not in rep


def test_ingest_ask_and_research_commands(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    g = lambda *a: subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True, text=True)
    g("init", "-q")
    (tmp_path / "README.md").write_text("# Thing\n\nIt does things.\n")
    g("add", "."); g("commit", "-q", "-m", "first")
    run(r, "init")
    run(r, "persona", "new", "critic", "--template", "critic")

    out = run(r, "ingest", "project", "--dry-run")
    assert "files read: README.md" in out and "dry run, would record" in out and "Top-level layout" in out
    out = run(r, "ingest", "project", "--adapter", "dry-run")
    assert "project -> shared" in out and "recorded" in out
    assert "README introduces" in run(r, "recall", "shared")

    out = run(r, "ingest", "history", "--adapter", "dry-run", "--since", "")
    assert "commits read: 1" in out and "history -> shared" in out

    (tmp_path / "call.md").write_text("They removed the bot after two weeks because of the noise.\n")
    out = run(r, "ingest", "notes", "call.md", "--who", "lead", "--origin", "call", "--persona", "critic", "--adapter", "dry-run", "--date", "2026-11-03")
    assert "human -> critic: 1 recorded" in out
    assert "lead" in run(r, "recall", "critic", "--query", "removed the bot")

    out = r.invoke(main, ["ask", "--who", "founder"], input="A CLI for reviews\n\nstop asking for tests\n\n\n", catch_exceptions=False)
    assert out.exit_code == 0 and "2 answers recorded" in out.output

    out = run(r, "research", "critic", "-q", "noisy bots?", "--dry-run")
    assert "research by critic -> critic: 0 recorded" in out and "dry run" in out


def test_decide_command(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    run(r, "init")
    run(r, "persona", "new", "critic", "--template", "critic")
    out = run(r, "decide", "The launch date does not move", "--who", "founder", "--origin", "planning meeting", "--date", "2026-11-04", "--why", "demo")
    assert "written under ## Decisions" in out and "[human" in out
    assert "The launch date does not move [founder, planning meeting] Reason: demo." in (tmp_path / ".rncrn8" / "world.md").read_text()
    (tmp_path / "plan.md").write_text("# Plan\n\nLaunch next week.\n")
    out = run(r, "review", "plan.md", "--dry-run")
    assert "The launch date does not move" in out and "settled" in out


def test_persona_panel_and_grouped_templates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    run(r, "init")
    out = run(r, "persona", "panel", "code")
    assert "created persona 'security'" in out and "created persona 'historian'" in out and "already here" not in out
    out = run(r, "persona", "panel", "code")
    assert "already here: security, maintainer, tester, operator, auditor, historian" in out and "created" not in out
    assert "6 personas: a review makes 6 review calls, 6 debate calls (each answering the other 5)" in out
    t = run(r, "persona", "templates")
    assert "Reading anything" in t and "Code" in t and "Video" in t and "The Auditor" in t
    assert "raises above belief 0.25" in t and "panels: code:" in t
    assert "auditor" in run(r, "persona", "list")
    # a persona whose file is present but broken is named, not silently counted as present
    (tmp_path / ".rncrn8" / "personas" / "tester" / "persona.yaml").write_text("name: [broken\n", encoding="utf-8")
    assert "present but unreadable" in run(r, "persona", "panel", "code")


def test_decisions_open_leaves_out_merged_and_conceded_findings(tmp_path, monkeypatch):
    from rncrn8.memory import MemoryStore, Workspace
    from rncrn8.memory.store import DecisionRecord

    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    run(r, "init")
    store = MemoryStore(Workspace.find(tmp_path))
    base = dict(persona="skimmer", prior=0.5, log_odds=0.0, belief=0.5, threshold=0.75, raise_finding=False, severity=0.5, review_id="r-x", where="§1", kind="problem")
    stands = store.append_decision(DecisionRecord(claim="the point that stands", **{**base, "persona": "critic", "raise_finding": True}))
    merged = store.append_decision(DecisionRecord(claim="the same point, folded in", withdrawn=True, withdrawn_how="merged into critic:F1", withdrawn_into=stands.id, **base))
    conceded = store.append_decision(DecisionRecord(claim="a point given up", withdrawn=True, withdrawn_how="conceded", **base))
    open_out = run(r, "decisions", "--open")
    assert stands.id in open_out and merged.id not in open_out and conceded.id not in open_out
    full = run(r, "decisions")
    assert f"merged into critic:F1 · belief 0.50 · closed with {stands.id}" in full and "conceded · belief 0.50 · not closable" in full


def test_doctor_names_what_is_wrong_and_what_to_do(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    r = CliRunner()
    res = r.invoke(main, ["doctor"], catch_exceptions=False)
    assert res.exit_code == 1 and "warn  workspace" in res.output and "fail  api key" in res.output and "sk-ant-api03" in res.output
    assert "1 problem above must be fixed" in res.output
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-not-a-real-key")
    out = run(r, "doctor")
    assert "`rncrn8 init` in the project folder creates one" in out and "then ask your assistant to set up rncrn8 (in Claude Code: /rncrn8:setup)" in out
    monkeypatch.delenv("ANTHROPIC_API_KEY")

    run(r, "init")
    run(r, "persona", "panel", "document")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-not-a-real-key")
    out = run(r, "doctor")
    lines = {l.split()[1] if l.split()[1] != "api" else "api key": l for l in out.splitlines() if l[:4] in ("ok  ", "warn", "fail", "skip")}
    assert lines["workspace"].startswith("ok") and lines["config"].startswith("ok") and "auditor, critic, skimmer, stranger (4)" in lines["personas"]
    assert lines["api key"].startswith("ok") and "present, sk-ant-api03…" in lines["api key"]
    assert lines["world"].startswith("warn") and lines["memory"].startswith("warn") and lines["model"].startswith("skip")
    assert out.rstrip().endswith("next: ask your assistant for a review (in Claude Code: /rncrn8:review)") and "warnings worth reading" in out

    out = run(r, "doctor", "--live", "--adapter", "dry-run")
    assert "ok    model         dry-run adapter answered; no model was called" in out

    (tmp_path / ".rncrn8" / "personas" / "critic" / "persona.yaml").write_text("name: [broken\n", encoding="utf-8")
    res = r.invoke(main, ["doctor"], catch_exceptions=False)
    assert res.exit_code == 1 and "fail  personas      unreadable: critic (" in res.output and "readable: auditor, skimmer, stranger" in res.output

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-usr-something-else")
    assert "ok    api key       present, sk-ant-usr-s…" in r.invoke(main, ["doctor"], catch_exceptions=False).output
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xyz-something-else")
    assert "warn  api key       present, sk-ant-xyz-s… (Anthropic API keys start with sk-ant-api or sk-ant-usr" in r.invoke(main, ["doctor"], catch_exceptions=False).output


def test_decide_does_not_record_the_same_decision_twice(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    run(r, "init")
    run(r, "decide", "rncrn8 is proprietary", "--who", "founder", "--origin", "chat")
    out = run(r, "decide", "rncrn8 is proprietary.", "--who", "founder", "--origin", "chat again")
    assert "already recorded as m-" in out and "nothing written" in out
    assert (tmp_path / ".rncrn8" / "world.md").read_text().count("rncrn8 is proprietary") == 1


def test_lists_stay_in_columns_with_a_long_persona_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    run(r, "init")
    run(r, "persona", "new", "critic", "--template", "critic")
    run(r, "persona", "new", "privacy-officer", "--template", "counsel")
    rows = [line for line in run(r, "persona", "list").splitlines() if "memories=" in line]
    assert len(rows) == 2 and len({row.index("memories=") for row in rows}) == 1
