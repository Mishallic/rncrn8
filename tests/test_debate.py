import re
from datetime import date

import pytest

from rncrn8.config import SourceType
from rncrn8.memory import MemoryStore
from rncrn8.models import DryRunAdapter, ModelError, ModelRequest, ModelResponse
from rncrn8.packet import packet_from_text
from rncrn8.persona import scaffold_persona
from rncrn8.review import run_review
from rncrn8.service import record_outcome, remember

TODAY = date(2026, 11, 5)
PACKET = packet_from_text("# Plan\n\nShip the review bot with ten findings per pull request.\n\n## Timing\n\nLaunch is next week.\n", "plan")

SUMMARY = "# X: summary\n\n## What I tend to get right\n- structure\n\n## What I was told to stop raising\n\n## What I am tracking\n- whether the bot gets muted\n"


@pytest.fixture
def panel(ws):
    scaffold_persona(ws, "stranger", template="stranger")
    human = remember(ws, "critic", "The team lead removed CodeRabbit after two weeks of noise", SourceType.HUMAN, "customer call", who="team lead", observed=TODAY)
    shared = remember(ws, "shared", "Launch date was fixed for next week by the founder", SourceType.PROJECT, "world.md", observed=TODAY)
    kept = remember(ws, "skimmer", "Pilot team two kept its review bot on and liked the volume", SourceType.HUMAN, "customer call", who="pilot team 2", observed=TODAY)
    return {"human": human, "shared": shared, "kept": kept}


def fids_in(text: str) -> set[str]:
    return set(re.findall(r"\[([a-z]+:F\d+)\]", text))


def make_script(mem, *, fail_debate_for: str | None = None):
    def script(request: ModelRequest) -> dict:
        me = request.label
        if "YOUR CURRENT SUMMARY" in request.user:
            return {"summary_markdown": SUMMARY.replace("X", me)}
        if "DEBATE ROUND" in request.user:
            if me == fail_debate_for:
                raise ModelError(f"{me}: boom")
            fids = fids_in(request.user)
            assert fids, "debate prompt must list finding ids"
            if me == "critic":
                assert "pilot team 2" not in request.system  # skimmer's memory is not in the critic's context
                return {
                    "positions": [
                        {"finding_id": "critic:F1", "action": "hold", "confidence": 0.75, "because": "evidence stands", "changed_by": [], "revised_claim": ""},
                        {"finding_id": "critic:F2", "action": "concede", "confidence": 0.2, "because": "the date is a settled decision", "changed_by": [mem["shared"].id], "revised_claim": ""},
                    ],
                    "responses": [{"finding_id": "skimmer:F1", "stance": "agree", "confidence": 0.8, "because": "I lost the thread too", "supports": [], "opposes": []}],
                    "new_findings": [],
                    "notes": "conceded F2",
                }
            if me == "skimmer":
                return {
                    "positions": [{"finding_id": "skimmer:F1", "action": "revise", "confidence": 0.7, "because": "sharper wording", "changed_by": ["critic:F1"], "revised_claim": "The first line does not say what the product is"}],
                    "responses": [
                        {"finding_id": "critic:F1", "stance": "disagree", "confidence": 0.6, "because": "another team kept the bot", "supports": [], "opposes": [mem["kept"].id, "m-notreal00000001"]},
                        {"finding_id": "critic:F2", "stance": "unsure", "confidence": 0.5, "because": "", "supports": [], "opposes": []},
                        {"finding_id": "critic:F1", "stance": "agree", "confidence": 0.9, "because": "duplicate response must be ignored", "supports": [], "opposes": []},
                        {"finding_id": "skimmer:F1", "stance": "agree", "confidence": 0.9, "because": "cannot respond to my own", "supports": [], "opposes": []},
                    ],
                    "new_findings": [],
                    "notes": "",
                }
            if me == "stranger":
                return {
                    "positions": [],
                    "responses": [
                        {"finding_id": "skimmer:F1", "stance": "agree", "confidence": 0.65, "because": "I did not know what it was either", "supports": [], "opposes": []},
                        {"finding_id": "critic:F1", "stance": "agree", "confidence": 0.5, "because": "weak hunch", "supports": [], "opposes": []},
                        {"finding_id": "ghost:F9", "stance": "agree", "confidence": 0.9, "because": "nonexistent", "supports": [], "opposes": []},
                    ],
                    "new_findings": [{"kind": "question", "claim": "Who is the reader of this plan?", "where": "§1", "rationale": "", "fix": "", "confidence": 0.7, "supports": [], "opposes": [], "prompted_by": ["skimmer:F1"]}],
                    "notes": "",
                }
        # round 1
        if me == "critic":
            return {"understood": "a plan", "notes": "", "findings": [
                {"kind": "problem", "claim": "Ten findings per pull request will get the bot muted", "where": "§1", "rationale": "noise", "fix": "cap at three", "confidence": 0.7, "supports": [mem["human"].id], "opposes": []},
                {"kind": "risk", "claim": "Launch next week is unrealistic", "where": "§2", "rationale": "", "fix": "", "confidence": 0.6, "supports": [], "opposes": []},
            ]}
        if me == "skimmer":
            return {"understood": "some plan", "notes": "", "findings": [
                {"kind": "risk", "claim": "Nothing in the first line says what this is", "where": "§1", "rationale": "", "fix": "", "confidence": 0.6, "supports": [], "opposes": []},
            ]}
        return {"understood": "no idea", "notes": "", "findings": []}

    return script


def test_debate_rescoring_statuses_and_persistence(ws, panel):
    adapter = DryRunAdapter(make_script(panel))
    out = run_review(ws, PACKET, adapter, panel=["critic", "skimmer", "stranger"], today=TODAY)
    r = out.review
    assert r.debated and r.summaries_updated
    labels = [req.label for req in adapter.requests]
    assert labels.count("critic") == 3 and labels.count("skimmer") == 3 and labels.count("stranger") == 3  # review, debate, summary
    assert set(out.prompts) >= {"critic", "critic:debate", "critic:summary"}
    assert out.prompts["critic:debate"][0] == out.prompts["critic"][0]  # same system prompt, so the cache hits

    by = {f.fid: f for p in r.personas for f in p.findings}

    f1 = by["critic:F1"]
    assert f1.action == "hold" and f1.final_confidence == 0.75
    assert [(x.persona, x.stance) for x in f1.responses] == [("skimmer", "disagree"), ("stranger", "agree")]
    assert f1.responses[0].opposes == [panel["kept"].id] and f1.responses[0].dropped_refs == ["m-notreal00000001"]
    assert f1.status == "disputed" and f1.conflict
    assert f1.belief != f1.belief_round1
    owners = {c["ref"]: c["persona"] for c in f1.contributions}
    assert owners[panel["kept"].id] == "skimmer" and owners[panel["human"].id] == "critic"
    assert owners[f1.responses[0].memory_id] == "skimmer"
    reading = [c for c in f1.contributions if c["ref"] == f1.memory_id][0]
    assert reading["dependence"] == pytest.approx(3 ** -0.5)  # three readings of one finding share a cluster
    stranger_hunch = [c for c in f1.contributions if c["label"] == "stranger agrees"][0]
    assert stranger_hunch["contribution"] == 0.0  # 0.5 is uninformative

    f2 = by["critic:F2"]
    assert f2.action == "concede" and f2.status == "dropped" and not f2.raise_finding
    assert f2.changed_by == [panel["shared"].id]
    assert all(c["label"] != "own reading" for c in f2.contributions)

    s1 = by["skimmer:F1"]
    assert s1.action == "revise" and s1.claim == "The first line does not say what the product is"
    assert s1.original_claim == "Nothing in the first line says what this is"
    assert s1.status == "agreed" and s1.raise_finding and s1.belief > 0.8 and s1.belief_round1 == pytest.approx(0.6)
    assert sorted(x.persona for x in s1.responses) == ["critic", "stranger"]

    new = by["stranger:F1"]
    assert new.round == 2 and new.prompted_by == ["skimmer:F1"] and new.status == "unchallenged"

    # persistence: decisions re-written in place, memories updated, responders remembered what they argued
    store = MemoryStore(ws)
    d1 = store.get_decision(f1.decision_id)
    assert d1.revision == 2 and not d1.withdrawn and d1.belief == pytest.approx(f1.belief)
    assert {c.get("persona") for c in d1.contributions} == {"critic", "skimmer", "stranger"}
    d2 = store.get_decision(f2.decision_id)
    assert d2.withdrawn and not d2.raise_finding
    conceded = store.get("critic", f2.memory_id)
    assert "conceded" in conceded.tags and "[conceded in debate" in conceded.note
    revised = store.get("skimmer", s1.memory_id)
    assert revised.claim == s1.claim and revised.confidence == pytest.approx(0.7)
    skimmer_debate = [e for e in store.load("skimmer") if "debate" in e.tags and "disagree" in e.tags]
    assert len(skimmer_debate) == 1 and skimmer_debate[0].claim.startswith("disagrees with critic [critic:F1]")
    assert len(store.load_decisions()) == 4  # 2 critic + 1 skimmer + 1 stranger (new)

    # summaries rewritten by each persona
    assert (ws.persona_dir("critic") / "summary.md").read_text().startswith("# critic: summary")
    assert "whether the bot gets muted" in (ws.persona_dir("skimmer") / "summary.md").read_text()
    assert "YOUR CURRENT SUMMARY" in out.prompts["critic:summary"][1] and "critic:F2" in out.prompts["critic:summary"][1]
    assert "agree (0.80) with skimmer" in out.prompts["critic:summary"][1]

    report = (out.review_dir / "report.md").read_text()
    for section in ("## Agreed (1)", "## Disputed (1)", "## Unchallenged (1)", "## Held (0)", "## Dropped (1)"):
        assert section in report, section
    assert "skimmer disagrees (0.60): another team kept the bot" in report
    assert "critic conceded because: the date is a settled decision" in report
    assert "skimmer revised (0.70)" in report and 'was: "Nothing in the first line' in report
    assert "review + debate + summaries updated" in report
    assert (out.review_dir / "prompts" / "critic.debate.user.txt").exists()
    assert (out.review_dir / "prompts" / "critic.summary.user.txt").exists()


def test_outcome_reaches_other_personas_witnesses(ws, panel):
    out = run_review(ws, PACKET, DryRunAdapter(make_script(panel)), panel=["critic", "skimmer", "stranger"], summaries=False, today=TODAY)
    f1 = next(f for p in out.review.personas for f in p.findings if f.fid == "critic:F1")
    store = MemoryStore(ws)
    disagree = f1.responses[0]
    before_skimmer = store.get("skimmer", disagree.memory_id).confidence
    before_kept = store.get("skimmer", panel["kept"].id).confidence
    before_human = store.get("critic", panel["human"].id).confidence

    res = record_outcome(ws, f1.decision_id, claim_true=True, note="the bot got muted")
    assert store.get("skimmer", disagree.memory_id).confidence < before_skimmer  # disagreed with a true claim
    assert store.get("skimmer", panel["kept"].id).confidence < before_kept
    assert store.get("critic", panel["human"].id).confidence > before_human
    assert "inference:skimmer" in res.origin_updates and "inference:critic" in res.origin_updates


def test_debate_prompt_shows_others_evidence_by_provenance_without_ids(ws, panel):
    out = run_review(ws, PACKET, DryRunAdapter(make_script(panel)), panel=["critic", "skimmer"], dry_run=True, today=TODAY)
    skimmer_debate = out.prompts["skimmer:debate"][1]
    assert "[critic:F1]" in skimmer_debate and "Ten findings per pull request" in skimmer_debate
    assert "(for: human 0.80, customer call, team lead)" in skimmer_debate
    assert panel["human"].id not in skimmer_debate  # the id is the critic's, not citable by the skimmer
    critic_debate = out.prompts["critic:debate"][1]
    assert panel["human"].id in critic_debate  # one's own ids are shown


def test_no_debate_flag_and_single_persona(ws, panel):
    out = run_review(ws, PACKET, DryRunAdapter(make_script(panel)), panel=["critic", "skimmer"], debate=False, summaries=False, today=TODAY)
    assert not out.review.debated
    statuses = {f.fid: f.status for p in out.review.personas for f in p.findings}
    assert statuses == {"critic:F1": "unchallenged", "critic:F2": "unchallenged", "skimmer:F1": "held"}
    assert "## Raised (2)" in (out.review_dir / "report.md").read_text()

    solo = run_review(ws, PACKET, DryRunAdapter(make_script(panel)), panel=["critic"], summaries=False, today=TODAY)
    assert not solo.review.debated and "critic:debate" not in solo.prompts


def test_debate_failure_for_one_persona_does_not_stop_the_others(ws, panel):
    out = run_review(ws, PACKET, DryRunAdapter(make_script(panel, fail_debate_for="stranger")), panel=["critic", "skimmer", "stranger"], summaries=False, today=TODAY)
    by_name = {p.persona: p for p in out.review.personas}
    assert by_name["stranger"].debate_error == "stranger: boom"
    by = {f.fid: f for p in out.review.personas for f in p.findings}
    assert by["critic:F2"].status == "dropped"  # the others' debate still applied
    assert [x.persona for x in by["skimmer:F1"].responses] == ["critic"]
    assert "debate failed: stranger: boom" in (out.review_dir / "report.md").read_text()


class EmptySummaryAdapter:
    name = "empty-summary"

    def __init__(self, inner):
        self.inner = inner

    def complete(self, request: ModelRequest) -> ModelResponse:
        if "YOUR CURRENT SUMMARY" in request.user:
            return ModelResponse(data={"summary_markdown": "   "}, text="", model="x")
        return self.inner.complete(request)


def test_empty_summary_leaves_file_alone(ws, panel):
    before = (ws.persona_dir("critic") / "summary.md").read_text()
    out = run_review(ws, PACKET, EmptySummaryAdapter(DryRunAdapter(make_script(panel))), panel=["critic", "skimmer"], today=TODAY)
    assert (ws.persona_dir("critic") / "summary.md").read_text() == before
    assert not out.review.summaries_updated
    assert all(p.summary_error == "model returned an empty summary" for p in out.review.personas)


def test_own_past_inferences_are_folded_not_counted(ws, panel):
    """A persona citing its own earlier findings is the same reader again: counted, not weighed."""
    from rncrn8.review.report import render_report

    past1 = remember(ws, "critic", "Earlier I found the plan too noisy", SourceType.INFERENCE, origin="inference:critic", confidence=0.7, observed=TODAY)
    past2 = remember(ws, "critic", "And I said so again", SourceType.INFERENCE, origin="inference:critic", confidence=0.8, observed=TODAY)
    other = remember(ws, "critic", "A colleague's note says the plan is too noisy", SourceType.HUMAN, "slack", observed=TODAY)

    def script(request: ModelRequest) -> dict:
        return {"understood": "", "notes": "", "prior_findings": [], "findings": [
            {"kind": "problem", "claim": "Too noisy", "where": "§1", "rationale": "", "fix": "", "confidence": 0.7, "supports": [past1.id, past2.id, other.id], "opposes": []}]}

    out = run_review(ws, PACKET, DryRunAdapter(script), panel=["critic"], dry_run=True, today=TODAY)
    f = out.review.personas[0].findings[0]
    refs = [c["ref"] for c in f.contributions]
    assert past1.id not in refs and past2.id not in refs and other.id in refs
    assert f.folded_past_readings == {"critic": 2}
    assert set(f.supports) == {past1.id, past2.id, other.id}  # still recorded as cited
    own = next(c for c in f.contributions if c["label"] == "own reading")
    assert own["dependence"] == 1.0  # one reader, counted once
    line = [l for l in render_report(out.review).splitlines() if "counted once" in l]
    assert line and "critic 2" in line[0]


def test_report_shows_decision_ids_and_unsure_wording(ws, panel):
    out = run_review(ws, PACKET, DryRunAdapter(make_script(panel)), panel=["critic", "skimmer"], summaries=False, today=TODAY)
    report = (out.review_dir / "report.md").read_text()
    f2 = next(f for p in out.review.personas for f in p.findings if f.fid == "critic:F2")
    assert f"decision: {f2.decision_id}" in report and "rncrn8 outcome " + f2.decision_id in report
    assert "skimmer is unsure (0.50)" in report and "unsures" not in report


def test_merge_folds_duplicate_into_the_other_reviewers_finding(ws, panel, monkeypatch):
    def script(request: ModelRequest) -> dict:
        me = request.label
        if "YOUR CURRENT SUMMARY" in request.user:
            return {"summary_markdown": SUMMARY}
        if "DEBATE ROUND" in request.user:
            if me == "skimmer":
                return {"positions": [{"finding_id": "skimmer:F1", "action": "merge", "confidence": 0.65, "because": "same point as the critic", "changed_by": [], "revised_claim": "", "duplicate_of": "critic:F1"}],
                        "responses": [{"finding_id": "critic:F1", "stance": "agree", "confidence": 0.9, "because": "ignored: already merged", "supports": [], "opposes": []}], "new_findings": [], "notes": ""}
            if me == "critic":
                return {"positions": [{"finding_id": "critic:F1", "action": "merge", "confidence": 0.7, "because": "mutual", "changed_by": [], "revised_claim": "", "duplicate_of": "skimmer:F1"}], "responses": [], "new_findings": [], "notes": ""}
            return {"positions": [], "responses": [], "new_findings": [], "notes": ""}
        if me == "critic":
            return {"understood": "", "notes": "", "prior_findings": [], "findings": [{"kind": "problem", "claim": "Ten per PR is too many", "where": "§1", "rationale": "", "fix": "", "confidence": 0.7, "supports": [panel["human"].id], "opposes": []}]}
        if me == "skimmer":
            return {"understood": "", "notes": "", "prior_findings": [], "findings": [{"kind": "problem", "claim": "Ten findings per PR will drown the reader", "where": "§1", "rationale": "", "fix": "", "confidence": 0.6, "supports": [panel["kept"].id], "opposes": []}]}
        return {"understood": "", "notes": "", "prior_findings": [], "findings": []}

    out = run_review(ws, PACKET, DryRunAdapter(script), panel=["critic", "skimmer"], summaries=False, today=TODAY)
    by = {f.fid: f for p in out.review.personas for f in p.findings}
    c, s = by["critic:F1"], by["skimmer:F1"]
    # a mutual merge is a cycle: the smallest fid survives, whatever order the personas were read in
    assert c.action == "hold" and c.merged_from == ["skimmer:F1"]
    assert s.action == "merge" and s.status == "merged" and s.merged_into == "critic:F1"
    agree = [r for r in c.responses if r.persona == "skimmer"]
    assert len(agree) == 1 and agree[0].stance == "agree" and agree[0].confidence == 0.65
    assert agree[0].supports == [panel["kept"].id] and agree[0].memory_id == s.memory_id
    assert c.status == "agreed" and c.belief > c.belief_round1
    labels = [x["label"] for x in c.contributions]
    assert "skimmer agrees" in labels and f"{panel['kept'].id} human (skimmer's)" in labels  # a cited memory names whose it is
    assert not s.raise_finding and all(x["label"] != "own reading" for x in s.contributions)
    store = MemoryStore(ws)
    merged = store.get_decision(s.decision_id)
    assert merged.withdrawn and merged.withdrawn_how == "merged into critic:F1" and merged.withdrawn_into == c.decision_id
    assert "merged" in store.get("skimmer", s.memory_id).tags
    report = (out.review_dir / "report.md").read_text()
    assert "## Merged (1)" in report and "[skimmer:F1] -> [critic:F1]" in report and "also raised as: skimmer:F1" in report
    assert "## Agreed (1)" in report

    # the listing and doctor count the way the report does: the merged finding is neither held nor waiting for an outcome
    from click.testing import CliRunner

    from rncrn8.cli import main
    from rncrn8.doctor import run_checks

    monkeypatch.chdir(ws.project_dir)
    listing = CliRunner().invoke(main, ["reviews", "list"], catch_exceptions=False).output
    assert "raised=1   held=0   merged=1 debated" in listing
    outcomes = next(c for c in run_checks(ws) if c.name == "outcomes")
    assert outcomes.detail.startswith("1 decision, none with an outcome")


def test_merge_into_unknown_or_own_finding_is_ignored(ws, panel):
    def script(request: ModelRequest) -> dict:
        if "DEBATE ROUND" in request.user:
            if request.label == "critic":
                return {"positions": [
                    {"finding_id": "critic:F1", "action": "merge", "confidence": 0.7, "because": "", "changed_by": [], "revised_claim": "", "duplicate_of": "ghost:F1"},
                    {"finding_id": "critic:F2", "action": "merge", "confidence": 0.7, "because": "", "changed_by": [], "revised_claim": "", "duplicate_of": "critic:F1"},
                ], "responses": [], "new_findings": [], "notes": ""}
            return {"positions": [], "responses": [], "new_findings": [], "notes": ""}
        if request.label == "critic":
            return {"understood": "", "notes": "", "prior_findings": [], "findings": [
                {"kind": "problem", "claim": "A", "where": "§1", "rationale": "", "fix": "", "confidence": 0.7, "supports": [], "opposes": []},
                {"kind": "problem", "claim": "B", "where": "§1", "rationale": "", "fix": "", "confidence": 0.7, "supports": [], "opposes": []}]}
        return {"understood": "", "notes": "", "prior_findings": [], "findings": []}

    out = run_review(ws, PACKET, DryRunAdapter(script), panel=["critic", "skimmer"], summaries=False, today=TODAY)
    by = {f.fid: f for p in out.review.personas for f in p.findings}
    assert by["critic:F1"].action == "hold" and by["critic:F2"].action == "hold"
    assert by["critic:F1"].status == "unchallenged"


def test_inference_memories_carry_target_tags_and_selection_prefers_them(ws, panel):
    from rncrn8.review.prompts import select_memories

    out = run_review(ws, PACKET, DryRunAdapter(make_script(panel)), panel=["critic", "skimmer"], summaries=False, today=TODAY)
    store = MemoryStore(ws)
    f = next(f for p in out.review.personas for f in p.findings if f.fid == "critic:F1")
    entry = store.get("critic", f.memory_id)
    assert "kind:document" in entry.tags and "target:plan" in entry.tags
    code_packet = packet_from_text("# Diff\n\nsome code change\n", "diff-x", kind="code")
    chosen = select_memories(store.load_visible("critic"), code_packet, limit=50, char_budget=100000, today=TODAY)
    # memories from the document review rank below the human memory with no kind tag
    assert chosen[0].id == panel["human"].id
    doc_chosen = select_memories(store.load_visible("critic"), PACKET, limit=50, char_budget=100000, today=TODAY)
    assert doc_chosen[0].id == f.memory_id


def _three_way(panel, positions_by_persona):
    """Round one: critic F1, skimmer F1, stranger F1 all on §1; the debate positions are supplied per persona."""

    def script(request: ModelRequest) -> dict:
        me = request.label
        if "DEBATE ROUND" in request.user:
            return {"positions": positions_by_persona.get(me, []), "responses": [], "new_findings": [], "notes": ""}
        return {"understood": "", "notes": "", "prior_findings": [], "findings": [
            {"kind": "problem", "claim": f"{me} says ten per PR is too many", "where": "§1", "rationale": "", "fix": "", "confidence": 0.7, "supports": [], "opposes": []}]}

    return script


def _merge(fid, into, conf=0.7):
    return {"finding_id": fid, "action": "merge", "confidence": conf, "because": "same point", "changed_by": [], "revised_claim": "", "duplicate_of": into}


def test_merge_chain_lands_on_the_surviving_finding_regardless_of_order(ws, panel):
    # critic -> skimmer, skimmer -> stranger: both must end up on stranger:F1
    script = _three_way(panel, {"critic": [_merge("critic:F1", "skimmer:F1")], "skimmer": [_merge("skimmer:F1", "stranger:F1", 0.6)]})
    out = run_review(ws, PACKET, DryRunAdapter(script), panel=["critic", "skimmer", "stranger"], summaries=False, today=TODAY)
    by = {f.fid: f for p in out.review.personas for f in p.findings}
    assert by["critic:F1"].merged_into == "stranger:F1" and by["skimmer:F1"].merged_into == "stranger:F1"
    assert sorted(by["stranger:F1"].merged_from) == ["critic:F1", "skimmer:F1"]
    assert sorted(r.persona for r in by["stranger:F1"].responses) == ["critic", "skimmer"]
    assert by["stranger:F1"].status == "agreed"


def test_merge_into_a_conceded_finding_keeps_the_merging_finding(ws, panel):
    concede = {"finding_id": "skimmer:F1", "action": "concede", "confidence": 0.2, "because": "I was wrong", "changed_by": ["critic:F1"], "revised_claim": "", "duplicate_of": ""}
    script = _three_way(panel, {"critic": [_merge("critic:F1", "skimmer:F1")], "skimmer": [concede]})
    out = run_review(ws, PACKET, DryRunAdapter(script), panel=["critic", "skimmer", "stranger"], summaries=False, today=TODAY)
    by = {f.fid: f for p in out.review.personas for f in p.findings}
    assert by["skimmer:F1"].status == "dropped"
    assert by["critic:F1"].action == "hold" and by["critic:F1"].merged_into is None and by["critic:F1"].raise_finding
    assert by["skimmer:F1"].merged_from == []


def test_one_persona_counts_once_however_many_findings_it_merges(ws, panel):
    def script(request: ModelRequest) -> dict:
        me = request.label
        if "DEBATE ROUND" in request.user:
            if me == "critic":
                return {"positions": [_merge("critic:F1", "skimmer:F1", 0.8), _merge("critic:F2", "skimmer:F1", 0.9)], "responses": [], "new_findings": [], "notes": ""}
            return {"positions": [], "responses": [], "new_findings": [], "notes": ""}
        if me == "critic":
            return {"understood": "", "notes": "", "prior_findings": [], "findings": [
                {"kind": "problem", "claim": "A", "where": "§1", "rationale": "", "fix": "", "confidence": 0.7, "supports": [], "opposes": []},
                {"kind": "problem", "claim": "B", "where": "§1", "rationale": "", "fix": "", "confidence": 0.7, "supports": [], "opposes": []}]}
        return {"understood": "", "notes": "", "prior_findings": [], "findings": [
            {"kind": "problem", "claim": "C", "where": "§1", "rationale": "", "fix": "", "confidence": 0.6, "supports": [], "opposes": []}]}

    out = run_review(ws, PACKET, DryRunAdapter(script), panel=["critic", "skimmer"], summaries=False, today=TODAY)
    by = {f.fid: f for p in out.review.personas for f in p.findings}
    target = by["skimmer:F1"]
    assert sorted(target.merged_from) == ["critic:F1", "critic:F2"]
    assert [r.persona for r in target.responses] == ["critic"]  # one agreement, not two
    assert sum(1 for c in target.contributions if c["label"] == "critic agrees") == 1


def test_objections_are_kept_and_shown(ws, panel):
    def script(request: ModelRequest) -> dict:
        me = request.label
        if "DEBATE ROUND" in request.user:
            if me == "skimmer":
                return {"positions": [], "responses": [{"finding_id": "critic:F1", "stance": "agree", "confidence": 0.7, "because": "yes", "objection": "The pilot team liked the volume, so noise may not be the problem here.", "supports": [], "opposes": []}], "new_findings": [], "notes": ""}
            return {"positions": [], "responses": [{"finding_id": "skimmer:F1", "stance": "agree", "confidence": 0.7, "because": "yes", "objection": "none", "supports": [], "opposes": []}], "new_findings": [], "notes": ""}
        return round1_for(me)

    def round1_for(me):
        return {"understood": "", "notes": "", "prior_findings": [], "findings": [{"kind": "problem", "claim": f"{me} point", "where": "§1", "rationale": "", "fix": "", "confidence": 0.7, "supports": [], "opposes": []}]}

    out = run_review(ws, PACKET, DryRunAdapter(script), panel=["critic", "skimmer"], summaries=False, today=TODAY)
    by = {f.fid: f for p in out.review.personas for f in p.findings}
    assert by["critic:F1"].responses[0].objection.startswith("The pilot team liked")
    assert by["skimmer:F1"].responses[0].objection == ""  # 'none' is kept out
    report = (out.review_dir / "report.md").read_text()
    assert "strongest objection: The pilot team liked the volume" in report
    assert "objection" in out.prompts["critic:debate"][1]
