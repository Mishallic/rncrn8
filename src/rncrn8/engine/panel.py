"""Combine personas with multiplicative weights (Hedge).

Each persona starts with equal weight. After each outcome, every persona that
read it is multiplied by exp(-eta * (loss - 0.25)), where 0.25 is the Brier loss
of saying one half: it gains if it beat a coin flip and loses if it did worse.
A persona that did not read the outcome counts as if it had said one half, so
saying nothing earns nothing and costs nothing. When every persona read the
outcome the shift cancels in the normalization and this is plain Hedge: the
panel's long-run error approaches that of the best single persona without
knowing in advance who that is.
Debate happens before this step and changes beliefs; Hedge only decides how
much each final belief counts. Disagreement is reported as spread, never hidden.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pydantic import BaseModel, Field

COIN_FLIP_BRIER = 0.25
"""The Brier loss of a reading of one half, whatever the outcome: the baseline a reading must beat to gain weight."""


def brier_loss(belief: float, outcome_true: bool) -> float:
    y = 1.0 if outcome_true else 0.0
    return (belief - y) ** 2


@dataclass(frozen=True)
class PanelVerdict:
    belief: float
    low: float
    high: float
    weights: dict[str, float]
    beliefs: dict[str, float]

    @property
    def spread(self) -> float:
        return self.high - self.low


class PanelWeights(BaseModel):
    model_config = {"frozen": True}

    weights: dict[str, float] = Field(default_factory=dict)

    @classmethod
    def uniform(cls, names: list[str]) -> "PanelWeights":
        if not names:
            return cls(weights={})
        w = 1.0 / len(names)
        return cls(weights={n: w for n in names})

    def normalized(self) -> "PanelWeights":
        total = sum(self.weights.values())
        if total <= 0:
            return PanelWeights.uniform(list(self.weights))
        return PanelWeights(weights={k: v / total for k, v in self.weights.items()})

    def with_personas(self, names: list[str]) -> "PanelWeights":
        """Add newcomers at the average current weight so they neither dominate nor vanish."""
        weights = dict(self.weights)
        missing = [n for n in names if n not in weights]
        if missing:
            avg = (sum(weights.values()) / len(weights)) if weights else 1.0
            for n in missing:
                weights[n] = avg
        return PanelWeights(weights=weights).normalized()

    def update(self, losses: dict[str, float], learning_rate: float, floor: float = 0.02) -> "PanelWeights":
        """Multiplicative update on each scored persona's loss minus a coin flip's, then a floor relative
        to uniform so a persona on a long bad streak can still recover when it improves (the fixed-share
        idea). Without the shift every outcome would lower whoever was scored relative to everyone who
        was not, since a loss is never negative: a persona that raises many findings and is right would
        sink below one that says nothing. floor=0.02 means no weight falls below two percent of its
        uniform share."""
        weights = dict(self.weights)
        for name, loss in losses.items():
            if name in weights and isinstance(loss, (int, float)) and math.isfinite(loss):
                weights[name] *= math.exp(-learning_rate * (loss - COIN_FLIP_BRIER))
        normalized = PanelWeights(weights=weights).normalized()
        if normalized.weights and floor > 0:
            # fixed share: mix a little uniform back in, so every weight is at least floor / n and the sum stays 1
            uniform = 1.0 / len(normalized.weights)
            return PanelWeights(weights={k: (1.0 - floor) * v + floor * uniform for k, v in normalized.weights.items()})
        return normalized

    def combine(self, beliefs: dict[str, float]) -> PanelVerdict:
        if not beliefs:
            raise ValueError("no beliefs to combine")
        active = self.with_personas(list(beliefs))
        used = {n: active.weights[n] for n in beliefs}
        total = sum(used.values())
        belief = sum(used[n] * beliefs[n] for n in beliefs) / total
        return PanelVerdict(
            belief=belief,
            low=min(beliefs.values()),
            high=max(beliefs.values()),
            weights=used,
            beliefs=dict(beliefs),
        )
