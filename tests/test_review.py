import json
import re
from datetime import date

import pytest

from rncrn8.config import SourceType
from rncrn8.memory import MemoryStore
from rncrn8.models import DryRunAdapter, ModelError, ModelRequest, ModelResponse
from rncrn8.packet import packet_from_text
from rncrn8.review import resolve_panel, run_review
from rncrn8.review.prompts import build_system_prompt, select_memories
from rncrn8.review.report import render_report
from rncrn8.service import remember

TODAY = date(2026, 11, 5)
PACKET = packet_from_text(
    "# Plan\n\nWe will ship the review bot with ten findings per pull request.\n\n## Timing\n\nLaunch is next week.\n",
    "plan",
)


@pytest.fixture
def seeded(ws):
    human = remember(ws, "critic", "The team lead removed CodeRabbit after two weeks of noise", SourceType.HUMAN, "customer call", who="team lead", observed=TODAY, tags=["noise"])
    forum = remember(ws, "critic", "Forum threads say review bots get muted when they post too much", SourceType.INTERNET, "https://forum", observed=TODAY, tags=["noise"])
    shared = remember(ws, "shared", "Launch date was fixed for next week by the founder", SourceType.PROJECT, "world.md", observed=TODAY)
    skim = remember(ws, "skimmer", "Viewers leave after three seconds without a hook", SourceType.HUMAN, "focus group", observed=TODAY)
    return {"human": human, "forum": forum, "shared": shared, "skim": skim}


def ids_in(text: str) -> set[str]:
    return set(re.findall(r"m-[0-9a-f]{15,}", text))


def scripted(seeded):
    def script(request: ModelRequest) -> dict:
        visible = ids_in(request.system)
        if request.label == "critic":
            return {
                "understood": "A launch plan for a review bot.",
                "findings": [
                    {"kind": "problem", "claim": "Ten findings per pull request will get the bot muted", "where": "§1", "rationale": "noise", "fix": "cap at three",
                     "confidence": 0.7, "supports": [seeded["human"].id, seeded["forum"].id, "m-doesnotexist0001"], "opposes": []},
                    {"kind": "question", "claim": "Is next week realistic?", "where": "§2", "rationale": "", "fix": "",
                     "confidence": 0.6, "supports": [], "opposes": [seeded["shared"].id]},
                    {"kind": "bogus-kind", "claim": "Kind gets normalised", "where": "§1", "rationale": "", "fix": "", "confidence": 1.7, "supports": [], "opposes": []},
                    {"claim": ""},
                ],
                "notes": "could not judge pricing",
            }
        if request.label == "skimmer":
            assert seeded["skim"].id in visible and seeded["human"].id not in visible  # isolation
            return {
                "understood": "Some plan.",
                "findings": [
                    {"kind": "risk", "claim": "Nothing in the first line says what this is", "where": "§1", "rationale": "", "fix": "",
                     "confidence": 0.55, "supports": [], "opposes": []},
                ],
                "notes": "",
            }
        return {"understood": "", "findings": [], "notes": ""}

    return script


def test_prompts_are_isolated_and_carry_provenance(ws, seeded):
    adapter = DryRunAdapter(scripted(seeded))
    out = run_review(ws, PACKET, adapter, dry_run=True, debate=False, today=TODAY)
    critic_sys, critic_user = out.prompts["critic"]
    skimmer_sys, _ = out.prompts["skimmer"]
    assert "The Critic" in critic_sys and "Reads the whole thing" in critic_sys
    assert seeded["human"].id in critic_sys and "human | confidence 0.80" in critic_sys and "customer call | team lead" in critic_sys
    assert seeded["shared"].id in critic_sys and seeded["shared"].id in skimmer_sys  # shared store visible to both
    assert seeded["skim"].id not in critic_sys and seeded["human"].id not in skimmer_sys
    assert "[§1] Plan" in critic_sys and "ten findings per pull request" in critic_sys and "KIND: document" in critic_sys
    assert critic_sys.startswith("REVIEW TARGET") and "----- cache boundary -----" in critic_sys
    assert critic_user.startswith("Review the target shown under REVIEW TARGET")
    assert "not instructions to you" in critic_sys
    assert len(adapter.requests) == 2 and {r.label for r in adapter.requests} == {"critic", "skimmer"}


def test_dry_run_records_nothing(ws, seeded):
    store = MemoryStore(ws)
    before = len(store.load("critic"))
    out = run_review(ws, PACKET, DryRunAdapter(scripted(seeded)), dry_run=True, today=TODAY)
    assert out.review_dir is None
    assert store.load_decisions() == []
    assert len(store.load("critic")) == before
    critic = next(p for p in out.review.personas if p.persona == "critic")
    assert critic.findings and all(f.decision_id is None and f.memory_id is None for f in critic.findings)
    report = render_report(out.review)
    health = report.split("## Panel health", 1)[1]
    assert "DRY RUN: no model was called" in report and "## Notes from the panel" not in report
    assert "memories shown" in health and " calls" not in health and "calls:" not in health


def test_full_round_scores_records_and_reports(ws, seeded):
    out = run_review(ws, PACKET, DryRunAdapter(scripted(seeded)), debate=False, summaries=False, today=TODAY)
    r = out.review
    assert out.review_dir is not None and (out.review_dir / "review.json").exists()
    assert (out.review_dir / "report.md").exists() and (out.review_dir / "prompts" / "critic.system.txt").exists()
    assert (out.review_dir / "packet.json").exists()

    critic = next(p for p in r.personas if p.persona == "critic")
    skimmer = next(p for p in r.personas if p.persona == "skimmer")
    assert critic.understood.startswith("A launch plan") and critic.notes == "could not judge pricing"
    assert critic.malformed == 1  # the empty claim
    assert len(critic.findings) == 3

    noise = next(f for f in critic.findings if "muted" in f.claim)
    assert noise.dropped_refs == ["m-doesnotexist0001"]
    assert set(noise.supports) == {seeded["human"].id, seeded["forum"].id}
    assert noise.memory_id is not None and noise.decision_id is not None
    assert noise.raise_finding and noise.belief > 0.8
    refs = [c["ref"] for c in noise.contributions]
    assert noise.memory_id in refs and seeded["human"].id in refs

    question = next(f for f in critic.findings if f.kind == "question")
    assert question.conflict  # own reading (0.6) supports, shared project memory (0.7) opposes
    assert question.opposes == [seeded["shared"].id]
    assert question.belief < 0.5  # the project fact outweighs the persona's reading
    assert question.raise_finding == (question.belief > question.threshold)  # critic's threshold is 1/3, so it still raises as a question

    normalised = next(f for f in critic.findings if "normalised" in f.claim)
    assert normalised.kind == "problem" and normalised.stated_confidence == 0.95

    # skimmer: one weak own-reading witness, threshold 0.75 -> held
    assert len(skimmer.findings) == 1 and not skimmer.findings[0].raise_finding
    assert skimmer.findings[0].belief == pytest.approx(0.55, abs=1e-6)


    # persistence: decisions carry the review id; inference memories exist
    store = MemoryStore(ws)
    decisions = store.load_decisions("critic")
    assert len(decisions) == 3 and all(d.review_id == r.id and d.outcome is None for d in decisions)
    inferences = [e for e in store.load("critic") if e.source == SourceType.INFERENCE]
    assert len(inferences) == 3 and all(r.id in e.tags and e.root == r.id for e in inferences)
    assert inferences[0].origin == "inference:critic"

    report = (out.review_dir / "report.md").read_text()
    assert "## Raised (" in report and "muted" in report and "## Held (1)" in report
    assert "invented memory ids dropped" in report and "CONFLICT" in report
    assert "could not judge pricing" in report


class RefusingAdapter:
    name = "refusing"

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.label == "skimmer":
            return ModelResponse(data=None, text="", model="x", stop_reason="refusal", refused=True, refusal_category="general_harms")
        if request.label == "critic":
            raise ModelError("critic: rate limited")
        return ModelResponse(data={"understood": "", "findings": [], "notes": ""}, text="{}", model="x")


def test_refusal_and_errors_do_not_stop_the_panel(ws, seeded):
    out = run_review(ws, PACKET, RefusingAdapter(), debate=False, summaries=False, today=TODAY)
    by = {p.persona: p for p in out.review.personas}
    assert by["skimmer"].refused and "general_harms" in by["skimmer"].refusal
    assert by["critic"].error == "critic: rate limited"
    report = (out.review_dir / "report.md").read_text()
    assert "declined" in report and "ERROR" in report


def test_resolve_panel_filters_by_kind_and_warns_on_named(ws):
    personas, warnings = resolve_panel(ws, None, "code")
    assert [p.name for p in personas] == ["critic"] and warnings == []  # skimmer: video, document, idea
    personas, warnings = resolve_panel(ws, ["skimmer", "critic"], "code")
    assert [p.name for p in personas] == ["skimmer", "critic"] and "skimmer" in warnings[0]
    with pytest.raises(ValueError):
        resolve_panel(ws, ["ghost"], "code")


def test_select_memories_prefers_relevant_and_respects_limits(ws):
    a = remember(ws, "critic", "pull request bots post too many findings", SourceType.HUMAN, "call", observed=TODAY)
    b = remember(ws, "critic", "tatreez patterns need a stitch chart", SourceType.INTERNET, "https://x", observed=TODAY)
    c = remember(ws, "critic", "launch timing slips", SourceType.INTERNET, "https://y", observed=date(2025, 1, 1))  # stale
    entries = MemoryStore(ws).load("critic")
    chosen = select_memories(entries, PACKET, limit=2, char_budget=10000, today=TODAY)
    assert [e.id for e in chosen][0] == a.id and len(chosen) == 2
    assert select_memories(entries, PACKET, limit=10, char_budget=10, today=TODAY) == []


def test_select_memories_never_cuts_the_personas_own_track_on_this_target(ws):
    """Nine findings from one review share a root, so the per-origin cap of six used to drop three, and the
    refuted one went first because its confidence had fallen. Judged entries come first, the track is exempt."""
    store = MemoryStore(ws)
    tags = ["r-x", "finding", "problem", f"kind:{PACKET.kind}", PACKET.target_tag]
    own = [remember(ws, "critic", f"finding {i} about pull request findings", SourceType.INFERENCE, "inference:critic", confidence=0.7, tags=tags, root="r-x", observed=TODAY) for i in range(8)]
    weak = remember(ws, "critic", "the launch date is a strength", SourceType.INFERENCE, "inference:critic", confidence=0.45, tags=tags, root="r-x", observed=TODAY)
    store.append(weak.model_copy(update={"contradicted_by": ["d-refuted"]}))
    noise = [remember(ws, "critic", f"forum post {i} about review bots and findings", SourceType.INTERNET, "https://forum", observed=TODAY) for i in range(8)]
    chosen = select_memories(store.load("critic"), PACKET, limit=40, char_budget=20000, today=TODAY)
    ids = [e.id for e in chosen]
    assert ids[0] == weak.id  # the refuted finding leads, whatever its score
    assert all(e.id in ids for e in own)  # the whole track is shown
    assert sum(1 for e in chosen if e.origin == "https://forum") == 6  # the cap still holds for everything else
    assert len(noise) == 8
    # another persona's inference on the same target is not this persona's track
    foreign = remember(ws, "critic", "someone else's finding about findings", SourceType.INFERENCE, "inference:skimmer", confidence=0.9, tags=tags, root="r-y", observed=TODAY)
    chosen = select_memories(store.load("critic"), PACKET, limit=40, char_budget=20000, today=TODAY, persona="critic")
    assert chosen[0].id == weak.id and foreign.id in [e.id for e in chosen] and [e.id for e in chosen].index(foreign.id) > 8
    # the track takes at most half the window, and what the rest leaves goes back to it
    for i in range(30):
        remember(ws, "critic", f"later finding {i} about pull request findings", SourceType.INFERENCE, "inference:critic", confidence=0.7, tags=tags, root=f"r-{i // 7}", observed=TODAY)
    chosen = select_memories(store.load("critic"), PACKET, limit=10, char_budget=20000, today=TODAY, persona="critic")
    assert len(chosen) == 10 and sum(1 for e in chosen if e.origin != "inference:critic") == 5  # half the window for the rest of memory
    only_track = [e for e in store.load("critic") if e.origin == "inference:critic"]
    assert len(select_memories(only_track, PACKET, limit=10, char_budget=20000, today=TODAY, persona="critic")) == 10  # nothing else to show, so the track fills it


def test_system_prompt_marks_stale_memory(ws):
    old = remember(ws, "critic", "old forum claim", SourceType.INTERNET, "https://z", observed=date(2025, 1, 1), expires=date(2025, 7, 1))
    from rncrn8.persona import load_persona

    prompt = build_system_prompt(load_persona(ws, "critic"), "world facts", "my summary", [old], 5, TODAY)
    assert "stale, re-check" in prompt and "world facts" in prompt and "my summary" in prompt and "at most 5" in prompt


def test_low_confidence_own_reading_counts_against(ws):
    """A persona that says 0.3 means it: the finding's belief is 0.3, under the critic's threshold, so it is held."""

    def script(conf: float):
        def run(request: ModelRequest) -> dict:
            return {"understood": "", "notes": "", "findings": [
                {"kind": "problem", "claim": "weak hunch", "where": "§1", "rationale": "", "fix": "", "confidence": conf, "supports": [], "opposes": []}]}

        return run

    out = run_review(ws, PACKET, DryRunAdapter(script(0.3)), panel=["critic"], summaries=False, today=TODAY)
    f = out.review.personas[0].findings[0]
    assert f.belief == pytest.approx(0.3) and f.contributions[0]["contribution"] < 0
    assert "## Held (1)" in render_report(out.review)  # 0.30 is under the critic's threshold of one third
    # at 0.4 the critic still raises it, and the evidence line says which way its reading counted
    out = run_review(ws, PACKET, DryRunAdapter(script(0.4)), panel=["critic"], summaries=False, today=TODAY)
    f = out.review.personas[0].findings[0]
    assert f.belief == pytest.approx(0.4) and "own reading, counts against 0.40 (-0.41)" in render_report(out.review)


def test_refuted_findings_are_shown_to_the_persona_and_count_against(ws):
    """Three findings at 0.6, each refuted by a person. The fourth review must show the critic the
    verdicts, and its reading at 0.6 must now count against its own claim."""
    import re as _re

    from rncrn8.service import record_outcome

    summary_seen: dict[str, str] = {}

    def script(request: ModelRequest) -> dict:
        if "YOUR CURRENT SUMMARY" in request.user:
            summary_seen.update(system=request.system, user=request.user)
            return {"summary_markdown": "# The Critic: summary\n\n## What I tend to get right\n\n## What I was told to stop raising\n- claim 0\n\n## What I am tracking\n"}
        prior = [{"memory_id": m, "status": "wrong", "because": "a person said so"} for m in _re.findall(r"^(m-[0-9a-f]+) \| inference", request.system, _re.M)]
        return {"understood": "", "notes": "", "prior_findings": prior, "findings": [
            {"kind": "problem", "claim": f"claim {len(prior)}", "where": "§1", "rationale": "", "fix": "", "confidence": 0.6, "supports": [], "opposes": []}]}

    for _ in range(3):
        out = run_review(ws, PACKET, DryRunAdapter(script), panel=["critic"], summaries=False, today=TODAY)
        record_outcome(ws, out.review.personas[0].findings[0].decision_id, False)
    out = run_review(ws, PACKET, DryRunAdapter(script), panel=["critic"], summaries=True, today=TODAY)
    system = out.prompts["critic"][0]
    # the summary step is told what the persona found about its earlier findings, and where a refuted one belongs
    assert "Your earlier findings on this target" in summary_seen["user"] and summary_seen["user"].count("outcome already recorded: refuted (explicit)") == 3
    assert all(f"refuted (explicit)): claim {i} (a person said so)" in summary_seen["user"] for i in range(3))  # the claim itself, not only the reason
    assert "4 of 4 memories shown" in render_report(out.review) or "3 of 3 memories shown" in render_report(out.review)
    assert "recorded as refuted belongs there" in summary_seen["system"]
    assert system.count("outcome: refuted, recorded by a person on") == 3
    assert "`refuted` means a person found the finding wrong" in system
    p = out.review.personas[0]
    f = p.findings[0]
    assert f.calibrated_confidence == pytest.approx(0.6 * (1 - 3 / 8))  # the 0.6 block: three misses, record counts 3/8
    assert f.belief == pytest.approx(f.calibrated_confidence) and f.contributions[0]["contribution"] < 0
    assert "own reading, counts against" in render_report(out.review)
    # the verdicts the persona echoed back do not re-close decisions a person already closed
    assert len(p.prior_findings) == 3 and all(pf.note == "outcome already recorded: refuted (explicit)" and not pf.recorded for pf in p.prior_findings)


class CachingDryRun(DryRunAdapter):
    supports_cache = True


def test_round_one_warms_the_shared_block_first(ws, seeded):
    adapter = CachingDryRun(scripted(seeded))
    out = run_review(ws, PACKET, adapter, dry_run=True, debate=False, today=TODAY)
    first = adapter.requests[0]
    assert first.label == "critic"  # the first persona ran alone before the others started
    assert first.system_blocks[0].startswith("REVIEW TARGET") and first.system_blocks[1].startswith("You are The Critic")
    assert all(r.system_blocks[0] == first.system_blocks[0] for r in adapter.requests)  # one shared block for every persona


def test_debate_reuses_the_review_system_blocks(ws, seeded):
    adapter = DryRunAdapter(scripted(seeded))
    run_review(ws, PACKET, adapter, dry_run=True, today=TODAY)
    by_label = {}
    for r in adapter.requests:
        by_label.setdefault(r.label, []).append(r)
    r1, r2 = by_label["critic"]
    assert r1.system_blocks == r2.system_blocks
    assert r1.schema == r2.schema  # the output schema is part of the cached prefix; both rounds share one
    assert set(r1.schema["required"]) == {"understood", "findings", "prior_findings", "positions", "responses", "new_findings", "notes"}
    assert "leave positions, responses and new_findings as empty arrays" in r1.user
    assert "leave understood as an empty string" in r2.user
    assert "DEBATE ROUND" in r2.user and "REVIEW TARGET" not in r2.user.split("DEBATE ROUND")[1][:0]
    assert "[§1]" not in r2.user  # the packet is not resent in the debate turn
