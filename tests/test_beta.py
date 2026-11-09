import pytest

from rncrn8.engine import BetaCounts


def test_mean_and_strength():
    b = BetaCounts(a=8, b=2)
    assert b.mean == pytest.approx(0.8)
    assert b.strength == 10


def test_updates_are_immutable_and_additive():
    b = BetaCounts(a=1, b=1)
    c = b.confirmed(0.5)
    d = c.contradicted(2.0)
    assert (b.a, b.b) == (1, 1)
    assert (c.a, c.b) == (1.5, 1)
    assert (d.a, d.b) == (1.5, 3)


def test_from_mean_keeps_strength():
    b = BetaCounts.from_mean(0.3, 10)
    assert b.mean == pytest.approx(0.3)
    assert b.strength == pytest.approx(10)


def test_recentered_keeps_strength_and_clamps():
    b = BetaCounts(a=7, b=3).recentered(1.0)
    assert b.strength == pytest.approx(10)
    assert 0.999 < b.mean < 1.0


def test_variance_shrinks_with_strength():
    weak = BetaCounts(a=2, b=2)
    strong = BetaCounts(a=20, b=20)
    assert strong.variance < weak.variance
