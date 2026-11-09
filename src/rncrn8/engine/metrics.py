"""Scores for probabilistic predictions against binary outcomes."""

from __future__ import annotations

import math
from dataclasses import dataclass

_EPS = 1e-6


def brier(preds: list[float], outcomes: list[bool]) -> float:
    if not preds:
        return float("nan")
    return sum((p - (1.0 if y else 0.0)) ** 2 for p, y in zip(preds, outcomes)) / len(preds)


def log_loss(preds: list[float], outcomes: list[bool]) -> float:
    if not preds:
        return float("nan")
    total = 0.0
    for p, y in zip(preds, outcomes):
        p = min(max(p, _EPS), 1.0 - _EPS)
        total += -math.log(p) if y else -math.log(1.0 - p)
    return total / len(preds)


def auc(preds: list[float], outcomes: list[bool]) -> float:
    """Probability a random true case scores above a random false case (ties count half). NaN if one class is missing."""
    pos = [p for p, y in zip(preds, outcomes) if y]
    neg = [p for p, y in zip(preds, outcomes) if not y]
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for p in pos:
        for q in neg:
            wins += 1.0 if p > q else (0.5 if p == q else 0.0)
    return wins / (len(pos) * len(neg))


def expected_calibration_error(preds: list[float], outcomes: list[bool], bins: int = 5) -> float:
    if not preds:
        return float("nan")
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for p, y in zip(preds, outcomes):
        idx = min(int(p * bins), bins - 1)
        buckets[idx].append((p, y))
    total = 0.0
    for b in buckets:
        if not b:
            continue
        conf = sum(p for p, _ in b) / len(b)
        acc = sum(1.0 for _, y in b if y) / len(b)
        total += len(b) / len(preds) * abs(conf - acc)
    return total


@dataclass(frozen=True)
class Scorecard:
    n: int
    brier: float
    log_loss: float
    auc: float
    ece: float

    @classmethod
    def of(cls, preds: list[float], outcomes: list[bool]) -> "Scorecard":
        return cls(n=len(preds), brier=brier(preds, outcomes), log_loss=log_loss(preds, outcomes), auc=auc(preds, outcomes), ece=expected_calibration_error(preds, outcomes))

    def as_dict(self) -> dict[str, float | int | None]:
        """Undefined metrics are None, so json.dumps writes null rather than the invalid bare token NaN."""

        def clean(x: float) -> float | None:
            return x if math.isfinite(x) else None

        return {"n": self.n, "brier": clean(self.brier), "log_loss": clean(self.log_loss), "auc": clean(self.auc), "ece": clean(self.ece)}
