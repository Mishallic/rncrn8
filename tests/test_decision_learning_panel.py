import pytest

from rncrn8.engine import LossMatrix, PanelWeights, Witness, attribute_outcome, brier_loss, combine, decide


def test_threshold_follows_loss_matrix():
    assert LossMatrix(false_alarm=1, miss=1).threshold == pytest.approx(0.5)
    assert LossMatrix(false_alarm=1, miss=4).threshold == pytest.approx(0.2)
    assert LossMatrix(false_alarm=3, miss=1).threshold == pytest.approx(0.75)


def test_same_belief_different_personas_different_decisions():
    belief = 0.4
    security = decide(belief, LossMatrix(false_alarm=1, miss=4))
    skimmer = decide(belief, LossMatrix(false_alarm=3, miss=1))
    assert security.raise_finding
    assert not skimmer.raise_finding
    assert security.severity == pytest.approx(0.4 * 4)


def test_attribution_shares_sum_to_learning_rate_and_follow_agreement():
    ev = combine(
        [Witness("a", 0.8, True), Witness("b", 0.6, True), Witness("c", 0.7, False)],
        prior=0.5,
    )
    updates = attribute_outcome(ev, claim_true=True, learning_rate=2.0)
    assert sum(u.amount for u in updates) == pytest.approx(2.0)
    by_ref = {u.ref: u for u in updates}
    assert by_ref["a"].agreed and by_ref["b"].agreed and not by_ref["c"].agreed
    assert by_ref["a"].share > by_ref["b"].share  # stronger witness carries more of the credit


def test_attribution_skips_uninformative_witnesses():
    ev = combine([Witness("weak", 0.5, True), Witness("ok", 0.8, True)], prior=0.5)
    updates = attribute_outcome(ev, claim_true=False)
    assert [u.ref for u in updates] == ["ok"]


def test_attribution_with_no_evidence_is_empty():
    assert attribute_outcome(combine([], 0.5), True) == []


def test_brier():
    assert brier_loss(1.0, True) == 0.0
    assert brier_loss(0.0, True) == 1.0
    assert brier_loss(0.5, False) == pytest.approx(0.25)


def test_hedge_moves_weight_toward_the_persona_that_is_right():
    weights = PanelWeights.uniform(["good", "bad"])
    for _ in range(20):
        # good says 0.9 when true; bad says 0.2
        losses = {"good": brier_loss(0.9, True), "bad": brier_loss(0.2, True)}
        weights = weights.update(losses, learning_rate=0.5)
    assert weights.weights["good"] > 0.9
    assert sum(weights.weights.values()) == pytest.approx(1.0)


def test_weight_follows_confidence_not_the_count_of_right_calls():
    # a tester saw a reviewer right 5 of 7 times outweigh one right 6 of 7: by design, when the first was surer when right
    weights = PanelWeights.uniform(["security", "critic"])
    security = [(0.9, True)] * 5 + [(0.6, False)] * 2  # (stated belief that the finding holds, what happened)
    critic = [(0.6, True)] * 6 + [(0.6, False)]
    for (s, s_true), (c, c_true) in zip(security, critic):
        weights = weights.update({"security": brier_loss(s, s_true), "critic": brier_loss(c, c_true)}, learning_rate=0.5)
    assert weights.weights["security"] > weights.weights["critic"]


def test_panel_combine_reports_spread_and_handles_newcomers():
    weights = PanelWeights.uniform(["a", "b"]).update({"b": 1.0}, learning_rate=2.0)
    verdict = weights.combine({"a": 0.8, "b": 0.2, "newcomer": 0.5})
    assert verdict.low == 0.2 and verdict.high == 0.8
    assert verdict.spread == pytest.approx(0.6)
    assert set(verdict.weights) == {"a", "b", "newcomer"}
    assert verdict.belief > 0.5  # a has most of the weight


def test_being_scored_is_no_penalty_against_saying_nothing():
    right, wrong = PanelWeights.uniform(["active", "quiet"]), PanelWeights.uniform(["active", "quiet"])
    for _ in range(30):  # the only persona scored, thirty times, at 0.86
        right = right.update({"active": brier_loss(0.86, True)}, learning_rate=0.5)
        wrong = wrong.update({"active": brier_loss(0.86, False)}, learning_rate=0.5)
    assert right.weights["active"] > 0.5 > wrong.weights["active"]  # right earns weight, wrong loses it, silence neither
    hedger = PanelWeights.uniform(["hedger", "quiet"]).update({"hedger": brier_loss(0.5, True)}, learning_rate=0.5)
    assert hedger.weights["hedger"] == pytest.approx(0.5)  # a coin flip earns nothing and costs nothing
    both = PanelWeights.uniform(["a", "b", "quiet"]).update({"a": 0.0, "b": 1.0}, learning_rate=0.5)
    assert both.weights["a"] > both.weights["quiet"] > both.weights["b"]


def test_panel_combine_requires_beliefs():
    with pytest.raises(ValueError):
        PanelWeights.uniform(["a"]).combine({})
