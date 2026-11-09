"""Only outcomes a person stands behind teach the track record; everything else is recorded and shown.

Also: self-checks stay on their own target, summaries keep their history, panels keep their cap,
a taken persona name is refused, proposals do not become constraints, and research facts that cite
a page the search never returned are kept only as weak leads."""

from datetime import date

import pytest

from rncrn8.config import SourceType
from rncrn8.memory import MemoryStore, ReliabilityRegistry
from rncrn8.models import DryRunAdapter, ModelRequest, ModelResponse
from rncrn8.outcome_source import FROM_CALLER, describe, teaches
from rncrn8.packet import packet_from_text
from rncrn8.replay import replay
from rncrn8.review import run_review
from rncrn8.service import record_outcome, remember
from rncrn8.track import PanelState

TODAY = date(2026, 11, 5)
PLAN = packet_from_text("# Plan\n\nShip it.\n", "plan")
PRICING = packet_from_text("# Pricing\n\nCharge more.\n", "pricing")


def _finding_with_memory(ws, claim="The plan is too noisy"):
    """A review whose one finding cites one shared memory, so an outcome has a memory and an origin to move."""
    fact = remember(ws, "shared", "Users ignore long reports", SourceType.INTERNET, "https://example.org/study", observed=TODAY)

    def script(request: ModelRequest) -> dict:
        return {"understood": "", "notes": "", "prior_findings": [], "findings": [
            {"kind": "problem", "claim": claim, "where": "§1", "rationale": "", "fix": "", "confidence": 0.7, "supports": [fact.id], "opposes": []}]}

    out = run_review(ws, PLAN, DryRunAdapter(script), panel=["critic"], summaries=False, today=TODAY)
    return out.review.personas[0].findings[0], fact


def test_the_rule():
    assert teaches(None) and teaches("explicit") and teaches("check")
    for s in ("loop", "backtest", "implicit:r-1", "implicit:commit:abc1234"):
        assert not teaches(s)
    assert FROM_CALLER == {"user": "explicit", "check": "check", "loop": "loop"}
    assert "a person" in describe(None) and "commit abc1234" in describe("implicit:commit:abc1234")
    assert "not confirmed" in describe("loop") and "check" in describe("check")


@pytest.mark.parametrize("source", ["loop", "implicit:r-test", "implicit:commit:abc1234", "backtest"])
def test_an_outcome_nobody_stands_behind_moves_no_trust(ws, source):
    f, fact = _finding_with_memory(ws)
    before = MemoryStore(ws).get("shared", fact.id).reliability
    res = record_outcome(ws, f.decision_id, True, source=source, learning_rate=0.5)
    after = MemoryStore(ws).get("shared", fact.id)
    assert after.reliability == before, "the memory's trust must not move"
    assert f.decision_id in after.confirmed_by, "but the link stays, so the outcome is visible"
    assert res.origin_updates == {} and ReliabilityRegistry(ws.reliability_path).get(fact.origin) is None
    assert res.panel_losses == {} and PanelState.load(ws.panel_path).weights == {}
    assert MemoryStore(ws).get_decision(f.decision_id).outcome_source == source


@pytest.mark.parametrize("source", ["explicit", "check"])
def test_an_outcome_a_person_stands_behind_teaches(ws, source):
    f, fact = _finding_with_memory(ws)
    before = MemoryStore(ws).get("shared", fact.id).reliability
    res = record_outcome(ws, f.decision_id, True, source=source)
    after = MemoryStore(ws).get("shared", fact.id)
    assert after.reliability.mean > before.mean and fact.origin in res.origin_updates
    assert res.panel_losses


def test_replay_tests_only_on_outcomes_that_teach(ws):
    for i in range(10):
        f, _ = _finding_with_memory(ws, f"claim {i}")
        record_outcome(ws, f.decision_id, True, source="explicit" if i < 6 else "loop")
    cur = replay(ws, holdout=0.5).results[0]
    assert cur.n_train + cur.n_test == 6  # the four loop outcomes are in neither set


def test_a_self_check_on_another_document_records_nothing(ws):
    first = run_review(ws, PLAN, DryRunAdapter(lambda r: {"understood": "", "notes": "", "prior_findings": [], "findings": [
        {"kind": "problem", "claim": "The plan is too noisy", "where": "§1", "rationale": "", "fix": "", "confidence": 0.7, "supports": [], "opposes": []}]}),
        panel=["critic"], summaries=False, today=TODAY)
    f = first.review.personas[0].findings[0]

    def says_addressed(request: ModelRequest) -> dict:
        return {"understood": "", "notes": "", "findings": [], "prior_findings": [{"memory_id": f.memory_id, "status": "addressed", "because": "looks fine"}]}

    other = run_review(ws, PRICING, DryRunAdapter(says_addressed), panel=["critic"], summaries=False, today=TODAY)
    pf = other.review.personas[0].prior_findings
    assert pf and not pf[0].recorded and "different target" in pf[0].note
    assert MemoryStore(ws).get_decision(f.decision_id).outcome is None

    same = run_review(ws, PLAN, DryRunAdapter(says_addressed), panel=["critic"], summaries=False, today=TODAY)
    assert same.review.personas[0].prior_findings[0].recorded


def test_a_summary_rewrite_keeps_the_old_text(ws):
    from rncrn8.review.summary import SUMMARY_HISTORY_DIR, write_summary

    d = ws.persona_dir("critic")
    (d / "summary.md").write_text("old lessons\n", encoding="utf-8")
    write_summary(ws, "critic", "new lessons\n", "r-1")
    write_summary(ws, "critic", "newer lessons\n", "r-2")
    assert (d / "summary.md").read_text() == "newer lessons\n"
    kept = sorted(p.read_text() for p in (d / SUMMARY_HISTORY_DIR).iterdir())
    assert kept == ["new lessons\n", "old lessons\n"]


def test_default_panels_hold_at_most_six(ws):
    from rncrn8.onboard import MAX_DEFAULT_PANEL, full_panels, join_panels, save_panels
    from rncrn8.persona import load_persona, scaffold_persona

    names = ["critic", "skimmer"]  # all seven below review documents
    for n in ("stranger", "auditor", "historian", "counsel", "customer"):
        scaffold_persona(ws, n, template=n)
        names.append(n)
    saved = save_panels(ws, {"document": names})
    assert saved["document"] == names[:6] and MAX_DEFAULT_PANEL == 6
    scaffold_persona(ws, "investor", template="investor")
    extra = load_persona(ws, "investor")
    assert full_panels(ws, extra) == ["document"]
    assert join_panels(ws, extra) == []
    assert ws.load_config().review.panels["document"] == names[:6]


def test_a_taken_name_is_refused_not_numbered(ws):
    from rncrn8.onboard import Brief, Proposal, write_personas

    item = {"name": "critic", "display_name": "Another Critic", "character": "x" * 80, "rubric": ["Is it right?", "Is it clear?"], "applies_to": ["*"]}

    class OneShot(DryRunAdapter):
        def complete(self, request):
            return ModelResponse(data={"personas": [item]}, text="", model="dry-run")

    made, problems = write_personas(OneShot(), Proposal(), [Brief("critic", "", "reviews plans")], {"critic", "skimmer"}, 2000)
    assert made == [] and "already exists" in problems[0]


def test_proposals_and_drafts_do_not_become_constraints():
    from rncrn8.onboard import _verified, not_binding

    assert not_binding("docs/proposals/loop-engineering.md") and not_binding("drafts/plan.md") and not_binding("proposal-pricing.md")
    assert not not_binding("README.md") and not not_binding("docs/decisions/0001-license.md") and not not_binding("docs/notes/meeting.md")
    texts = {"docs/proposals/x.md": "rncrn8 runs only on Claude.", "README.md": "The package must never be uploaded to PyPI."}
    rejected: list[str] = []
    kept = _verified([{"text": "Runs only on Claude.", "source": "docs/proposals/x.md", "quote": "rncrn8 runs only on Claude."},
                      {"text": "Never upload to PyPI.", "source": "README.md", "quote": "must never be uploaded to PyPI"}],
                     "text", texts, 8, "constraint", rejected)
    assert [s.source for s in kept] == ["README.md"] and "proposal or a draft" in rejected[0]


def test_research_marks_facts_whose_page_the_search_never_returned(ws, monkeypatch):
    import importlib

    research_mod = importlib.import_module("rncrn8.sources.research")
    from rncrn8.sources.extract import Fact

    def fake_extract(adapter, system, user, max_facts, label=None, tools=None):
        facts = [Fact("A seen fact.", "https://seen.example/a", "Seen", None, 0.8), Fact("An unseen fact.", "https://unseen.example/b", "Unseen", None, 0.8)]
        return facts, "notes", None, [{"url": "https://seen.example/a?utm=1"}]

    monkeypatch.setattr(research_mod, "extract_facts", fake_extract)
    res = research_mod.research(ws, DryRunAdapter(), "critic", "q?", budget=1, shared=True)
    by = {f.origin: f for f in res.facts}
    assert research_mod.UNSEEN_URL_TAG in by["https://unseen.example/b"].tags
    assert by["https://unseen.example/b"].confidence == pytest.approx(research_mod.UNSEEN_URL_CONFIDENCE)
    assert research_mod.UNSEEN_URL_TAG not in by["https://seen.example/a"].tags and by["https://seen.example/a"].confidence == 0.8


# --- after the panel's review of the release ------------------------------------------------------


@pytest.mark.parametrize("verdict", [True, False])
def test_a_person_can_replace_an_unconfirmed_outcome_but_not_the_reverse(ws, verdict):
    f, fact = _finding_with_memory(ws)
    record_outcome(ws, f.decision_id, not verdict, source="loop")
    mem = MemoryStore(ws).get("shared", fact.id)
    assert (f.decision_id in mem.contradicted_by) == verdict and mem.reliability == fact.reliability
    res = record_outcome(ws, f.decision_id, verdict, source="explicit")
    assert res.replaced == "loop" and res.updated and res.panel_losses
    mem = MemoryStore(ws).get("shared", fact.id)
    assert (f.decision_id in mem.confirmed_by) == verdict and (f.decision_id in mem.contradicted_by) != verdict
    with pytest.raises(ValueError, match="already has an outcome"):
        record_outcome(ws, f.decision_id, True, source="loop")
    with pytest.raises(ValueError, match="already has an outcome"):
        record_outcome(ws, f.decision_id, True, source="explicit")


def test_an_unconfirmed_refutation_reaches_prompts_as_unconfirmed(ws):
    from rncrn8.track import outcome_notes

    f, _ = _finding_with_memory(ws)
    record_outcome(ws, f.decision_id, False, source="loop")
    notes = outcome_notes(MemoryStore(ws).load_decisions())
    line = next(iter(notes.values()))
    assert "unconfirmed" in line and "no person has judged it" in line


def test_research_roots_ignore_scheme_www_and_trailing_slash():
    import importlib

    root = importlib.import_module("rncrn8.sources.research")._root
    assert root("https://www.example.org/a/") == root("http://example.org/a") == "example.org/a"
    assert root("https://example.org/a") != root("https://example.org/invented-page")


def test_the_mcp_tools_say_who_stands_behind_each_outcome(ws):
    import asyncio
    import json

    from mcp.server.mcpserver.exceptions import ToolError

    from rncrn8.mcp_server import build_server

    def call(server, name, **args):
        res = asyncio.run(server.call_tool(name, args))
        data = getattr(res, "structured_content", None)
        if data is None:
            data = json.loads(res.content[0].text)
        return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data

    f, _ = _finding_with_memory(ws)
    server = build_server(ws.project_dir)
    with pytest.raises(ToolError):
        call(server, "answer", answers=[{"decision_id": f.decision_id, "right": True, "reason": "fine"}])
    out = call(server, "answer", answers=[{"decision_id": f.decision_id, "right": True, "reason": "the loop says so", "source": "loop"}])
    rec = out["recorded"][0]
    assert rec["teaches"] is False and rec["trust_moved"] == [] and rec["memories_linked"] == 2  # the cited fact and the persona's own reading
    assert any(s["decision_id"] == f.decision_id for s in out["still_open"])
    assert PanelState.load(ws.panel_path).weights == {}


def test_the_cli_outcome_needs_a_source_outside_a_terminal(ws, monkeypatch):
    from click.testing import CliRunner

    from rncrn8.cli import main

    f, _ = _finding_with_memory(ws)
    monkeypatch.chdir(ws.project_dir)
    runner = CliRunner()
    missing = runner.invoke(main, ["outcome", f.decision_id, "--confirmed"])
    assert missing.exit_code != 0 and "--source" in missing.output
    looped = runner.invoke(main, ["outcome", f.decision_id, "--confirmed", "--source", "loop"])
    assert looped.exit_code == 0 and "no trust or weight moved" in looped.output and "-> conf" not in looped.output
    person = runner.invoke(main, ["outcome", f.decision_id, "--refuted", "--source", "user"])
    assert person.exit_code == 0 and "replaced an unconfirmed outcome (loop)" in person.output


def test_plugin_package_and_pin_share_one_version():
    import json
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pkg = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    plugin = json.loads((root / "plugin/.claude-plugin/plugin.json").read_text())["version"]
    mcp = json.loads((root / "plugin/.mcp.json").read_text())["mcpServers"]["rncrn8"]["args"]
    from rncrn8 import __version__

    assert pkg == plugin == __version__ and f"rncrn8=={pkg}" in mcp
