"""Combine witnesses into a belief.

Bayesian updating under conditional independence is a sum of log-likelihood
ratios. Each witness contributes its weight of evidence, decayed by age and
discounted when it is not independent of other witnesses (ten posts that
repeat one study are one witness, not ten).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

_EPS = 1e-9


def log_odds(p: float) -> float:
    p = min(max(p, _EPS), 1.0 - _EPS)
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass(frozen=True)
class EvidenceParams:
    """Tunable constants for evidence combination. All live in config.yaml."""

    max_witness_weight: float = 4.0
    """Clip on one witness's log-odds so a near-certain source cannot drown the rest."""

    allow_negative_reliability: bool = False
    """If false, a witness with reliability <= 0.5 contributes nothing instead of
    counting as evidence for the opposite. A vendor blog saying X is not evidence
    against X; it is just weak."""

    dependence_exponent: float = 0.5
    """Witnesses in the same cluster are discounted by n ** -exponent. At 0.5 the
    cluster's total evidence grows like sqrt(n), the standard effective-sample-size
    correction. 1.0 collapses a cluster to one witness; 0.0 treats them as independent."""

    stale_factor: float = 0.3
    """Multiplier on a witness past its review date until it is re-checked."""

    max_cluster_weight: float = 8.0
    """Cap on the total log-odds one dependence cluster may contribute. A cluster is one source
    however many times it speaks; two maximal witnesses' worth is the ceiling."""


@dataclass(frozen=True)
class Witness:
    """One memory's testimony about one claim."""

    ref: str
    reliability: float
    supports: bool
    age_days: float = 0.0
    halflife_days: float | None = None
    cluster: str | None = None
    stale: bool = False
    scale: float = 1.0
    """Extra multiplier on this witness's weight, e.g. a persona's panel weight. 1.0 means none."""
    allow_negative: bool = False
    """A persona's reading: its confidence is a probability, and after calibration an observed hit
    rate, so below one half it counts against the claim. A cited source below one half is merely weak."""


@dataclass(frozen=True)
class Contribution:
    ref: str
    supports: bool
    reliability: float
    weight: float
    decay: float
    dependence: float
    stale_factor: float
    contribution: float
    """Signed log-odds actually added: +/- weight * decay * dependence * stale_factor * scale."""
    scale: float = 1.0


@dataclass(frozen=True)
class Evidence:
    prior: float
    log_odds: float
    belief: float
    contributions: tuple[Contribution, ...] = field(default_factory=tuple)

    @property
    def supporting(self) -> tuple[Contribution, ...]:
        """Live witnesses that take the claim's side. A reading calibrated below one half still belongs
        here even though its contribution is negative: stance and arithmetic are different things."""
        return tuple(c for c in self.contributions if c.supports and c.contribution != 0.0)

    @property
    def opposing(self) -> tuple[Contribution, ...]:
        return tuple(c for c in self.contributions if not c.supports and c.contribution != 0.0)

    @property
    def evidence_for(self) -> float:
        """Net log-odds from the supporting side; negative when its readings count against."""
        return sum(c.contribution for c in self.supporting)

    @property
    def evidence_against(self) -> float:
        return -sum(c.contribution for c in self.opposing)

    @property
    def conflict(self) -> bool:
        """True when live witnesses take both stances. Must be reported, never averaged away. Two
        reviewers agreeing is not a conflict, whatever the arithmetic makes of one of them."""
        return bool(self.supporting) and bool(self.opposing)

    def ranked(self) -> list[Contribution]:
        return sorted(self.contributions, key=lambda c: abs(c.contribution), reverse=True)


def weight_of_evidence(reliability: float, params: EvidenceParams, allow_negative: bool = False) -> float:
    """Unsigned log-likelihood ratio of a witness with the given reliability. A reliability that is
    not a finite number is garbage, and garbage is uninformative, never maximal."""
    if not math.isfinite(reliability):
        return 0.0
    r = min(max(reliability, _EPS), 1.0 - _EPS)
    if not (params.allow_negative_reliability or allow_negative) and r <= 0.5:
        return 0.0
    w = math.log(r / (1.0 - r))
    return max(-params.max_witness_weight, min(params.max_witness_weight, w))


def decay_factor(age_days: float, halflife_days: float | None) -> float:
    """exp(-ln2 * age / halflife), i.e. 2 ** (-age / halflife). No halflife: no decay."""
    if halflife_days is None or halflife_days <= 0 or not math.isfinite(age_days) or age_days <= 0:
        return 1.0
    return 2.0 ** (-age_days / halflife_days)


def dependence_factors(witnesses: list[Witness], exponent: float) -> dict[int, float]:
    """Discount per witness index from the size of its cluster. Unclustered witnesses are unique."""
    keys = [w.cluster if w.cluster is not None else f"__solo__{i}" for i, w in enumerate(witnesses)]
    sizes = Counter(keys)
    return {i: sizes[k] ** (-exponent) for i, k in enumerate(keys)}


def combine(witnesses: list[Witness], prior: float, params: EvidenceParams | None = None) -> Evidence:
    params = params or EvidenceParams()
    L0 = log_odds(prior)
    deps = dependence_factors(witnesses, params.dependence_exponent)
    keys = [w.cluster if w.cluster is not None else f"__solo__{i}" for i, w in enumerate(witnesses)]
    raw: list[tuple[Witness, float, float, float, float, float]] = []
    for i, w in enumerate(witnesses):
        weight = weight_of_evidence(w.reliability, params, w.allow_negative)
        decay = decay_factor(w.age_days, w.halflife_days)
        dep = deps[i]
        stale = params.stale_factor if w.stale else 1.0
        scale = w.scale if math.isfinite(w.scale) and w.scale >= 0 else 1.0
        signed = weight * decay * dep * stale * scale * (1.0 if w.supports else -1.0)
        raw.append((w, weight, decay, dep, stale, signed))
    # a cluster is one source: cap what it may add in total, scaling its members down together
    totals: dict[str, float] = {}
    for k, (_, _, _, _, _, signed) in zip(keys, raw):
        totals[k] = totals.get(k, 0.0) + abs(signed)
    factors = {k: (params.max_cluster_weight / t if t > params.max_cluster_weight else 1.0) for k, t in totals.items()}
    contributions: list[Contribution] = []
    L = L0
    for k, (w, weight, decay, dep, stale, signed) in zip(keys, raw):
        signed *= factors[k]
        L += signed
        contributions.append(
            Contribution(
                ref=w.ref,
                supports=w.supports,
                reliability=w.reliability,
                weight=weight,
                decay=decay,
                dependence=dep * factors[k],
                stale_factor=stale,
                contribution=signed,
                scale=w.scale,
            )
        )
    belief = min(max(sigmoid(L), 1e-6), 1.0 - 1e-6)
    return Evidence(prior=prior, log_odds=L, belief=belief, contributions=tuple(contributions))
