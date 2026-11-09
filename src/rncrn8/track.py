"""Track records: what each persona's readings said, and what happened.

Two things are learned from closed decisions. A calibrator per persona maps stated
confidence to observed hit rate. Hedge weights per persona move with the Brier loss
of each reading; a persona whose readings keep missing counts for less.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, Field

from .config import CalibrationSettings
from .engine import Calibrator, PanelWeights, brier_loss
from .memory.store import DecisionRecord
from .outcome_source import describe, teaches


def is_reading(c: dict) -> bool:
    """A persona's fresh judgement on a finding: its own reading, or an agree/disagree in the debate."""
    if c.get("role") == "reading":
        return True
    label = c.get("label", "")
    return label == "own reading" or label.endswith(" agrees") or label.endswith(" disagrees")


def counts_for_learning(rec: DecisionRecord) -> bool:
    """Only outcomes a person stands behind teach calibration and panel weights (see outcome_source).
    A self-check is a persona grading itself, and a commit touching cited lines can only ever confirm, so a
    record built from them would call every persona nearly infallible; loop and backtest outcomes would
    credit the panel for being obeyed or for fitting history."""
    return rec.outcome is not None and teaches(rec.outcome_source)


def reading_points(decisions: list[DecisionRecord]) -> dict[str, list[tuple[float, bool]]]:
    """Per persona: (stated confidence, was right) for every reading on a decision a person closed."""
    points: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for rec in decisions:
        if not counts_for_learning(rec):
            continue
        for c in rec.contributions:
            if not is_reading(c):
                continue
            persona = c.get("persona", rec.persona)
            stated = float(c.get("stated", c["reliability"]))
            points[persona].append((stated, bool(c["supports"]) == bool(rec.outcome)))
    return dict(points)


def outcome_notes(decisions: list[DecisionRecord]) -> dict[str, str]:
    """Per reading memory id, one line on the outcome the decision it belongs to received, for the
    prompt: a persona must know a person judged its finding wrong before it decides to raise it again."""
    notes: dict[str, str] = {}
    for rec in decisions:
        if rec.outcome is None:
            continue
        verdict = "confirmed" if rec.outcome else "refuted"
        when = rec.outcome_at.date().isoformat() if rec.outcome_at else "earlier"
        how = describe(rec.outcome_source)
        if not teaches(rec.outcome_source):
            # an outcome nobody stands behind is context, never a judgement: it must not make a persona stop raising a finding
            verdict = f"marked {verdict} but unconfirmed, no person has judged it,"
        for c in rec.contributions:
            if not is_reading(c) or not isinstance(c.get("ref"), str):
                continue
            label = str(c.get("label", ""))
            if label.endswith(" agrees"):
                notes[c["ref"]] = f"the finding you agreed with was {verdict}, {how} on {when} (decision {rec.id})"
            elif label.endswith(" disagrees"):
                notes[c["ref"]] = f"the finding you disagreed with was {verdict}, {how} on {when} (decision {rec.id})"
            else:
                notes[c["ref"]] = f"{verdict}, {how} on {when} (decision {rec.id})"
    return notes


def fit_calibrators(decisions: list[DecisionRecord], settings: CalibrationSettings) -> dict[str, Calibrator]:
    return {name: Calibrator(pts, settings.prior_strength, settings.min_points) for name, pts in reading_points(decisions).items()}


class PanelState(BaseModel):
    """Hedge weights and running stats per persona. Stored in .rncrn8/panel.json."""

    weights: dict[str, float] = Field(default_factory=dict)
    n: dict[str, int] = Field(default_factory=dict)
    brier_sum: dict[str, float] = Field(default_factory=dict)
    right: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "PanelState":
        if not path.exists():
            return cls()
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def losses_for(self, rec: DecisionRecord, outcome: bool) -> dict[str, float]:
        """Brier loss of each persona's reading(s) on this decision, averaged when it read twice."""
        acc: dict[str, list[float]] = defaultdict(list)
        for c in rec.contributions:
            if not is_reading(c):
                continue
            persona = c.get("persona", rec.persona)
            r = float(c.get("stated", c["reliability"]))
            belief = r if c["supports"] else 1.0 - r
            acc[persona].append(brier_loss(belief, outcome))
        return {k: sum(v) / len(v) for k, v in acc.items()}

    def update_from_decision(self, rec: DecisionRecord, outcome: bool, learning_rate: float) -> dict[str, float]:
        losses = self.losses_for(rec, outcome)
        if not losses:
            return {}
        weights = PanelWeights(weights=self.weights).with_personas(list(losses)).update(losses, learning_rate)
        self.weights = weights.weights
        for persona, loss in losses.items():
            self.n[persona] = self.n.get(persona, 0) + 1
            self.brier_sum[persona] = self.brier_sum.get(persona, 0.0) + loss
            if loss < 0.25:  # the reading was on the right side of a coin flip
                self.right[persona] = self.right.get(persona, 0) + 1
        return losses

    def scale_for(self, persona: str, influence: float) -> float:
        """Multiplier on a persona's reading witnesses: (weight / uniform) ** influence; 1.0 when unknown."""
        if persona not in self.weights or len(self.weights) < 2 or influence <= 0:
            return 1.0
        uniform = 1.0 / len(self.weights)
        return (self.weights[persona] / uniform) ** influence

    def mean_brier(self, persona: str) -> float | None:
        n = self.n.get(persona, 0)
        return (self.brier_sum.get(persona, 0.0) / n) if n else None
