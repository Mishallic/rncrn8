import math

import pytest

from rncrn8.engine import EvidenceParams, Witness, combine
from rncrn8.engine.evidence import decay_factor, dependence_factors, log_odds, sigmoid, weight_of_evidence


def w(ref, r, supports=True, **kw):
    return Witness(ref=ref, reliability=r, supports=supports, **kw)


def test_sigmoid_inverts_log_odds():
    for p in (0.01, 0.3, 0.5, 0.9):
        assert sigmoid(log_odds(p)) == pytest.approx(p)


def test_no_witnesses_returns_prior():
    ev = combine([], prior=0.3)
    assert ev.belief == pytest.approx(0.3)
    assert not ev.conflict


def test_weight_is_log_likelihood_ratio_and_clipped():
    params = EvidenceParams(max_witness_weight=2.0)
    assert weight_of_evidence(0.8, params) == pytest.approx(math.log(4))
    assert weight_of_evidence(0.9999, params) == 2.0


def test_reliability_at_or_below_half_is_uninformative_by_default():
    params = EvidenceParams()
    assert weight_of_evidence(0.5, params) == 0.0
    assert weight_of_evidence(0.3, params) == 0.0
    # and the tunable flips it into evidence for the opposite
    neg = EvidenceParams(allow_negative_reliability=True)
    assert weight_of_evidence(0.3, neg) < 0


def test_symmetric_evidence_leaves_prior_unchanged_and_flags_conflict():
    ev = combine([w("a", 0.8, True), w("b", 0.8, False)], prior=0.5)
    assert ev.belief == pytest.approx(0.5)
    assert ev.conflict
    assert ev.evidence_for == pytest.approx(ev.evidence_against)


def test_stronger_source_wins_a_conflict():
    ev = combine([w("human", 0.8, True), w("blog", 0.57, False)], prior=0.5)
    assert ev.belief > 0.5
    assert ev.ranked()[0].ref == "human"


def test_decay_halves_at_halflife():
    assert decay_factor(0, 100) == 1.0
    assert decay_factor(100, 100) == pytest.approx(0.5)
    assert decay_factor(200, 100) == pytest.approx(0.25)
    assert decay_factor(500, None) == 1.0


def test_old_evidence_counts_less():
    fresh = combine([w("a", 0.8, True, age_days=0, halflife_days=180)], prior=0.5)
    old = combine([w("a", 0.8, True, age_days=360, halflife_days=180)], prior=0.5)
    assert old.belief < fresh.belief
    assert old.contributions[0].decay == pytest.approx(0.25)


def test_dependence_discount_is_sqrt_of_cluster_size():
    ws = [w("a", 0.8, cluster="study"), w("b", 0.8, cluster="study"), w("c", 0.8, cluster="study"), w("d", 0.8, cluster="study")]
    deps = dependence_factors(ws, 0.5)
    assert all(d == pytest.approx(0.5) for d in deps.values())
    # unclustered witnesses are independent
    solo = dependence_factors([w("a", 0.8), w("b", 0.8)], 0.5)
    assert all(d == 1.0 for d in solo.values())


def test_ten_copies_of_one_study_are_not_ten_witnesses():
    independent = combine([w(f"i{k}", 0.8, True) for k in range(10)], prior=0.5)
    copies = combine([w(f"c{k}", 0.8, True, cluster="one-study") for k in range(10)], prior=0.5)
    single = combine([w("s", 0.8, True)], prior=0.5)
    assert single.belief < copies.belief < independent.belief
    # total evidence of the cluster grows like sqrt(n)
    assert copies.log_odds == pytest.approx(single.log_odds * math.sqrt(10))


def test_exponent_one_collapses_cluster_to_one_witness():
    params = EvidenceParams(dependence_exponent=1.0)
    copies = combine([w(f"c{k}", 0.8, True, cluster="x") for k in range(5)], prior=0.5, params=params)
    single = combine([w("s", 0.8, True)], prior=0.5, params=params)
    assert copies.log_odds == pytest.approx(single.log_odds)


def test_stale_witness_is_discounted():
    live = combine([w("a", 0.8, True)], prior=0.5)
    stale = combine([w("a", 0.8, True, stale=True)], prior=0.5, params=EvidenceParams(stale_factor=0.3))
    assert stale.contributions[0].contribution == pytest.approx(live.contributions[0].contribution * 0.3)


def test_a_reading_below_one_half_counts_against_but_a_cited_source_does_not():
    from rncrn8.engine.evidence import Witness, combine

    source = Witness(ref="m", reliability=0.3, supports=True)
    reading = Witness(ref="r", reliability=0.3, supports=True, allow_negative=True)
    assert combine([source], 0.5).belief == pytest.approx(0.5)
    ev = combine([reading], 0.5)
    assert ev.belief == pytest.approx(0.3) and ev.contributions[0].contribution < 0
    # a disagreement by a persona wrong more often than right at that confidence counts for the claim
    assert combine([Witness(ref="d", reliability=0.3, supports=False, allow_negative=True)], 0.5).belief == pytest.approx(0.7)



def test_a_reading_that_counts_against_is_not_a_conflict():
    from rncrn8.engine.evidence import Witness, combine

    agree_but_wrong_often = Witness(ref="r", reliability=0.4, supports=True, allow_negative=True)
    ev = combine([Witness(ref="m", reliability=0.8, supports=True), agree_but_wrong_often], 0.5)
    assert ev.contributions[1].contribution < 0 and not ev.conflict  # two supporters, whatever the arithmetic
    ev = combine([Witness(ref="m", reliability=0.8, supports=True), Witness(ref="o", reliability=0.7, supports=False)], 0.5)
    assert ev.conflict


def test_conflict_and_the_two_sides_follow_stances_not_signs():
    from rncrn8.engine.evidence import Witness, combine

    # the mirror case: a disagreeing reader wrong more often than right at 0.3 pushes for the claim, and that is a conflict of stances
    ev = combine([Witness(ref="m", reliability=0.8, supports=True), Witness(ref="d", reliability=0.3, supports=False, allow_negative=True)], 0.5)
    assert ev.contributions[1].contribution > 0 and ev.conflict
    assert [c.ref for c in ev.supporting] == ["m"] and [c.ref for c in ev.opposing] == ["d"]
    assert ev.evidence_against == pytest.approx(-ev.contributions[1].contribution)  # negative: the opposing side pushed the other way
    # a supporter that counts against lowers the supporting side's net, and is not listed as opposing
    ev = combine([Witness(ref="m", reliability=0.8, supports=True), Witness(ref="r", reliability=0.4, supports=True, allow_negative=True)], 0.5)
    assert len(ev.supporting) == 2 and ev.opposing == () and ev.evidence_for < ev.contributions[0].contribution
