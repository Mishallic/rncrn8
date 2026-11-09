import math
from datetime import date

import pytest

from rncrn8.config import EngineConfig, SourceType
from rncrn8.engine import Calibrator, Scorecard
from rncrn8.engine.calibration import pav
from rncrn8.engine.metrics import auc, brier, expected_calibration_error, log_loss
from rncrn8.memory import MemoryStore
from rncrn8.models import DryRunAdapter, ModelRequest
from rncrn8.packet import packet_from_text
from rncrn8.replay import replay
from rncrn8.review import run_review
from rncrn8.service import record_outcome, remember, resolve_decision_id
from rncrn8.track import PanelState, fit_calibrators, reading_points

TODAY = date(2026, 11, 5)
PACKET = packet_from_text("# Plan\n\nShip it.\n", "plan")


# --- calibration ----------------------------------------------------------------------------


def test_pav_is_monotone_and_pools_violators():
    blocks = pav([(0.2, 1.0), (0.4, 0.0), (0.6, 1.0), (0.8, 1.0)])
    means = [b.mean for b in blocks]
    assert means == sorted(means)
    assert blocks[0].lo == 0.2 and blocks[0].hi == 0.4 and blocks[0].mean == pytest.approx(0.5)


def test_pav_pools_identical_x_into_one_block_whatever_the_order():
    """Twenty-nine hits and one miss, all at 0.6, in the worst order: one block with hit rate 29/30."""
    pts = [(0.6, 0.0)] + [(0.6, 1.0)] * 29
    blocks = pav(pts)
    assert len(blocks) == 1 and blocks[0].n == 30 and blocks[0].mean == pytest.approx(29 / 30)
    cal = Calibrator([(0.6, False)] + [(0.6, True)] * 29, prior_strength=5)
    assert cal(0.6) > 0.85
    # ties at two levels still give a monotone fit
    blocks = pav([(0.3, 1.0), (0.3, 0.0), (0.3, 0.0), (0.8, 1.0), (0.8, 1.0), (0.8, 0.0)])
    assert [round(b.mean, 3) for b in blocks] == [0.333, 0.667]


def test_calibrator_identity_with_few_points_and_shrinkage():
    assert Calibrator([(0.7, True)], prior_strength=5, min_points=3).identity
    assert Calibrator([(0.7, True)], prior_strength=5, min_points=3)(0.7) == 0.7
    # a persona that says 0.6 and is always right gets pulled up, but not all the way with few points
    pts = [(0.6, True)] * 4
    cal = Calibrator(pts, prior_strength=4, min_points=3)
    assert cal.alpha == pytest.approx(0.5)
    assert cal(0.6) == pytest.approx(0.5 * 1.0 + 0.5 * 0.6)
    many = Calibrator([(0.6, True)] * 400, prior_strength=4)
    assert many(0.6) > 0.98
    # output stays in (0, 1) and is monotone across the grid
    ys = [y for _, y in many.curve()]
    assert all(0.0 < y < 1.0 for y in ys) and ys == sorted(ys)


def test_calibrator_shrinks_each_block_by_its_own_count():
    """Three misses at 0.6 and one hit at 0.76: the miss block's record counts 3/8, the hit block's only 1/6."""
    cal = Calibrator([(0.6, False)] * 3 + [(0.76, True)], prior_strength=5, min_points=3)
    assert cal(0.6) == pytest.approx(0.6 * (1 - 3 / 8))
    assert cal(0.76) == pytest.approx(0.76 + (1 / 6) * (1 - 0.76))
    # above the stated range the lift fades, and it is the one-outcome block's lift, not the whole record's
    assert cal(0.9) == pytest.approx(0.9 + (cal(0.76) - 0.76) * 0.3)
    assert cal(0.9) < 0.9 + (4 / 9) * (1 - 0.76) * 0.3
    ys = [y for _, y in cal.curve()]
    assert ys == sorted(ys)
    # neighbours with the same hit rate are one block, so three misses in a row count as a record of three
    blocks = pav([(0.55, 0.0), (0.6, 0.0), (0.7, 0.0), (0.755, 1.0)])
    assert [(b.lo, b.hi, b.n) for b in blocks] == [(0.55, 0.7, 3), (0.755, 0.755, 1)]
    assert Calibrator([(0.55, False), (0.6, False), (0.7, False), (0.755, True)], prior_strength=5)(0.6) == pytest.approx(0.6 * (1 - 3 / 8))
    # a sparse later block never dips under a dense earlier one
    dense_then_sparse = Calibrator([(0.5, True)] * 8 + [(0.5, False)] * 2 + [(0.55, True)], prior_strength=5)
    assert dense_then_sparse(0.55) >= dense_then_sparse(0.5) == pytest.approx(0.5 + (10 / 15) * 0.3)


def test_calibrator_interpolates_between_blocks():
    pts = [(0.3, False)] * 5 + [(0.9, True)] * 5
    cal = Calibrator(pts, prior_strength=0.0001)
    assert cal.isotonic(0.3) == pytest.approx(0.0)
    assert cal.isotonic(0.9) == pytest.approx(1.0)
    assert cal.isotonic(0.6) == pytest.approx(0.5)
    assert cal.isotonic(0.1) == pytest.approx(0.0) and cal.isotonic(0.99) == pytest.approx(1.0)


# --- metrics --------------------------------------------------------------------------------


def test_metrics():
    assert brier([1.0, 0.0], [True, False]) == 0.0
    assert brier([0.5], [True]) == pytest.approx(0.25)
    assert log_loss([0.5, 0.5], [True, False]) == pytest.approx(math.log(2))
    assert auc([0.9, 0.8, 0.2, 0.1], [True, True, False, False]) == 1.0
    assert auc([0.1, 0.9], [True, False]) == 0.0
    assert math.isnan(auc([0.5], [True]))
    assert expected_calibration_error([0.9, 0.9], [True, True]) == pytest.approx(0.1)
    card = Scorecard.of([0.9, 0.1], [True, False])
    assert card.n == 2 and card.auc == 1.0 and card.brier == pytest.approx(0.01)


# --- track record from decisions ------------------------------------------------------------


def _script(conf: float, claim: str = "Too noisy"):
    def script(request: ModelRequest) -> dict:
        return {"understood": "", "notes": "", "prior_findings": [], "findings": [
            {"kind": "problem", "claim": claim, "where": "§1", "rationale": "", "fix": "", "confidence": conf, "supports": [], "opposes": []}]}

    return script


def test_reading_points_and_calibration_feed_the_next_review(ws):
    store = MemoryStore(ws)
    # five reviews where the critic says 0.6 and is right every time
    for i in range(5):
        out = run_review(ws, PACKET, DryRunAdapter(_script(0.6, f"claim {i}")), panel=["critic"], summaries=False, today=TODAY)
        did = out.review.personas[0].findings[0].decision_id
        record_outcome(ws, did, True)
    pts = reading_points(store.load_decisions())
    assert pts["critic"] == [(0.6, True)] * 5
    cals = fit_calibrators(store.load_decisions(), ws.load_config().calibration)
    assert cals["critic"](0.6) > 0.6

    out = run_review(ws, PACKET, DryRunAdapter(_script(0.6, "claim next")), panel=["critic"], summaries=False, today=TODAY)
    f = out.review.personas[0].findings[0]
    assert f.stated_confidence == 0.6 and f.calibrated_confidence > 0.6
    own = next(c for c in f.contributions if c["label"] == "own reading")
    assert own["stated"] == 0.6 and own["reliability"] == pytest.approx(f.calibrated_confidence)
    assert own["role"] == "reading" and "age_days" in own and "cluster" in own
    assert out.review.personas[0].calibration_points == 5
    report = (out.review_dir / "report.md").read_text()
    assert "0.60 stated" in report and "calibrated" in report and "calibration on 5 outcomes" in report
    # the inference memory entry starts at the calibrated mean
    assert store.get("critic", f.memory_id).confidence == pytest.approx(f.calibrated_confidence)


def test_panel_weights_move_with_outcomes_and_scale_readings(ws):
    from rncrn8.persona import scaffold_persona

    scaffold_persona(ws, "stranger", template="stranger")

    def script(request: ModelRequest) -> dict:
        if "DEBATE ROUND" in request.user:
            if request.label == "stranger":
                return {"positions": [], "responses": [{"finding_id": "critic:F1", "stance": "disagree", "confidence": 0.9, "because": "no", "supports": [], "opposes": []}], "new_findings": [], "notes": ""}
            return {"positions": [{"finding_id": "critic:F1", "action": "hold", "confidence": 0.8, "because": "", "changed_by": [], "revised_claim": ""}], "responses": [], "new_findings": [], "notes": ""}
        if request.label == "critic":
            return _script(0.8)(request)
        return {"understood": "", "notes": "", "prior_findings": [], "findings": []}

    out = run_review(ws, PACKET, DryRunAdapter(script), panel=["critic", "stranger"], summaries=False, today=TODAY)
    f = out.review.personas[0].findings[0]
    res = record_outcome(ws, f.decision_id, True)  # critic right at 0.8, stranger wrong at 0.9
    assert set(res.panel_losses) == {"critic", "stranger"}
    assert res.panel_losses["critic"] < res.panel_losses["stranger"]
    state = PanelState.load(ws.panel_path)
    assert state.weights["critic"] > state.weights["stranger"]
    assert state.n == {"critic": 1, "stranger": 1} and state.right == {"critic": 1}
    assert state.scale_for("critic", 0.5) > 1.0 > state.scale_for("stranger", 0.5)
    assert state.scale_for("critic", 0.0) == 1.0 and state.scale_for("nobody", 0.5) == 1.0

    out2 = run_review(ws, PACKET, DryRunAdapter(script), panel=["critic", "stranger"], summaries=False, today=TODAY)
    f2 = out2.review.personas[0].findings[0]
    own = next(c for c in f2.contributions if c["label"] == "own reading")
    stranger = next(c for c in f2.contributions if c["label"] == "stranger disagrees")
    assert own["scale"] > 1.0 > stranger["scale"]
    assert out2.review.personas[0].panel_weight == pytest.approx(state.weights["critic"])
    assert "panel weight x" in (out2.review_dir / "report.md").read_text()


def test_outcome_by_review_finding_id(ws):
    out = run_review(ws, PACKET, DryRunAdapter(_script(0.7)), panel=["critic"], summaries=False, today=TODAY)
    rid = out.review.id
    did = out.review.personas[0].findings[0].decision_id
    assert resolve_decision_id(ws, f"{rid}:critic:F1") == did
    res = record_outcome(ws, f"{rid}:critic:F1", False, note="nope")
    assert res.record.id == did and res.record.outcome is False and res.record.outcome_source == "explicit"
    with pytest.raises(KeyError):
        resolve_decision_id(ws, f"{rid}:critic:F9")
    with pytest.raises(KeyError):
        resolve_decision_id(ws, "nonsense")


def test_prior_findings_become_implicit_outcomes(ws):
    first = run_review(ws, PACKET, DryRunAdapter(_script(0.7, "The plan is too noisy")), panel=["critic"], summaries=False, today=TODAY)
    f = first.review.personas[0].findings[0]
    other = remember(ws, "critic", "not my finding", SourceType.HUMAN, "call", observed=TODAY)

    def script(request: ModelRequest) -> dict:
        assert f.memory_id in request.system and "tags: " in request.system
        return {"understood": "", "notes": "", "findings": [], "prior_findings": [
            {"memory_id": f.memory_id, "status": "addressed", "because": "the cap is in place now"},
            {"memory_id": other.id, "status": "wrong", "because": "not mine"},
            {"memory_id": "m-ghost0000000000", "status": "addressed", "because": ""},
        ]}

    second = run_review(ws, PACKET, DryRunAdapter(script), panel=["critic"], summaries=False, today=TODAY)
    pfs = second.review.personas[0].prior_findings
    assert [p.status for p in pfs] == ["addressed", "wrong", "addressed"]
    assert pfs[0].recorded and pfs[0].decision_id == f.decision_id
    assert not pfs[1].recorded and "not one of this persona's own findings" in pfs[1].note
    assert not pfs[2].recorded
    store = MemoryStore(ws)
    closed = store.get_decision(f.decision_id)
    assert closed.outcome is True and closed.outcome_source == f"implicit:{second.review.id}"
    assert closed.outcome_learning_rate == pytest.approx(ws.load_config().outcomes.implicit_learning_rate)
    assert "the cap is in place now" in closed.outcome_note
    report = (second.review_dir / "report.md").read_text()
    assert "## Follow-ups on earlier findings" in report and f"confirmed {f.decision_id}" in report

    # dry run never records
    third = run_review(ws, PACKET, DryRunAdapter(script), panel=["critic"], dry_run=True, today=TODAY)
    assert not any(p.recorded for p in third.review.personas[0].prior_findings)


# --- replay ---------------------------------------------------------------------------------


def test_replay_scores_current_and_candidate_on_holdout(ws):
    for i in range(10):
        out = run_review(ws, PACKET, DryRunAdapter(_script(0.6 if i % 2 else 0.8, f"c{i}")), panel=["critic"], summaries=False, today=TODAY)
        record_outcome(ws, out.review.personas[0].findings[0].decision_id, i % 3 != 0)
    report = replay(ws, holdout=0.3)
    assert len(report.results) == 1
    cur = report.results[0]
    assert cur.n_train == 7 and cur.n_test == 3 and cur.n_skipped == 0
    assert cur.recorded.n == 3 and cur.replayed.n == 3
    assert not math.isnan(cur.replayed.brier)

    candidate = EngineConfig.model_validate({"prior_belief": 0.9, "calibration": {"enabled": False}, "panel": {"apply_weights": False}})
    report = replay(ws, candidate, holdout=0.3)
    assert [r.label for r in report.results] == ["current", "candidate"]
    assert report.results[1].replayed.brier != report.results[0].replayed.brier


def test_replay_without_learning_scores_the_same_holdout_with_nothing_fitted(ws):
    for i in range(10):
        out = run_review(ws, PACKET, DryRunAdapter(_script(0.6 if i % 2 else 0.8, f"c{i}")), panel=["critic"], summaries=False, today=TODAY)
        record_outcome(ws, out.review.personas[0].findings[0].decision_id, i % 3 != 0)
    report = replay(ws, holdout=0.3, without_learning=True)
    assert [r.label for r in report.results] == ["current", "no-learning"]
    cur, base = report.results
    assert cur.n_test == base.n_test == 3 and base.replayed.n == 3
    assert not math.isnan(base.replayed.brier) and base.replayed.brier != cur.replayed.brier  # the calibration fitted on 7 changes the score


def test_learning_is_judged_with_an_interval_and_only_on_enough_decisions(ws):
    from rncrn8.replay import compare_learning

    # a persona that says 0.9 and is right one time in three: calibration on the older outcomes should fix that
    for i in range(60):
        out = run_review(ws, PACKET, DryRunAdapter(_script(0.9, f"c{i}")), panel=["critic"], summaries=False, today=TODAY)
        record_outcome(ws, out.review.personas[0].findings[0].decision_id, i % 3 == 0)
    report = replay(ws, holdout=0.4, without_learning=True)
    cmp = compare_learning(*report.results)
    assert cmp.n == 24 and cmp.verdict == "helped" and cmp.high < 0 and cmp.low <= cmp.difference <= cmp.high
    assert report.as_dict()["learning_vs_none"]["verdict"] == "helped"
    small = replay(ws, holdout=0.1, without_learning=True)
    assert compare_learning(*small.results).verdict.startswith("too few held-out decisions (6)")


def test_replay_with_no_or_few_decisions(ws):
    assert "no closed decisions" in replay(ws).notes[0]
    out = run_review(ws, PACKET, DryRunAdapter(_script(0.7)), panel=["critic"], summaries=False, today=TODAY)
    record_outcome(ws, out.review.personas[0].findings[0].decision_id, True)
    report = replay(ws)
    assert report.results[0].n_train == 0 and report.results[0].n_test == 1
    assert any("no train/test split" in n for n in report.notes)


def test_implicit_outcomes_do_not_feed_calibration_or_weights(ws):
    from rncrn8.memory import DecisionRecord
    from rncrn8.track import PanelState, reading_points

    store = MemoryStore(ws)
    reading = {"ref": "self", "supports": True, "reliability": 0.6, "weight": 0.4, "decay": 1.0, "dependence": 1.0, "stale_factor": 1.0, "contribution": 0.4, "label": "own reading", "persona": "critic", "role": "reading", "stated": 0.6}
    for i in range(6):
        store.append_decision(DecisionRecord(persona="critic", claim=f"i{i}", prior=0.5, log_odds=0.4, belief=0.6, threshold=0.33, raise_finding=True, severity=1.2, contributions=[reading], outcome=True, outcome_source="implicit:commit:abc"))
    store.append_decision(DecisionRecord(persona="critic", claim="e", prior=0.5, log_odds=0.4, belief=0.6, threshold=0.33, raise_finding=True, severity=1.2, contributions=[reading], outcome=False, outcome_source="explicit"))
    pts = reading_points(store.load_decisions())
    assert pts["critic"] == [(0.6, False)]  # six confirm-only implicit outcomes are not evidence of being right

    # and the panel ignores implicit outcomes at record time
    out = run_review(ws, PACKET, DryRunAdapter(_script(0.7)), panel=["critic"], summaries=False, today=TODAY)
    did = out.review.personas[0].findings[0].decision_id
    res = record_outcome(ws, did, True, source="implicit:r-test", learning_rate=0.5)
    assert res.panel_losses == {} and PanelState.load(ws.panel_path).weights == {}


def test_calibration_fades_outside_the_stated_range():
    cal = Calibrator([(0.65, True)] * 20 + [(0.7, True)] * 20, prior_strength=5)
    inside = cal(0.68)
    assert inside > 0.9  # within the range the record applies in full
    assert cal(0.5) < 0.75  # a confidence the persona has never stated is mostly taken at its word
    assert cal(0.3) == pytest.approx(0.3, abs=0.02)  # far outside, the correction has faded away
    assert cal(0.95) >= cal(0.7) and cal(0.95) >= 0.95  # above the range the curve never drops below its edge value, nor below the stated value
    ys = [cal(x / 20) for x in range(21)]
    assert all(0.0 < y < 1.0 for y in ys)
