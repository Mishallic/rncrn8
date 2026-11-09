"""Decide by expected cost, not by belief alone.

A security persona has a high miss cost. A skimmer persona has a high false
alarm cost, because noise is what kills adoption. Same belief, different
decisions, on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field


class LossMatrix(BaseModel):
    model_config = {"frozen": True}

    false_alarm: float = Field(default=1.0, gt=0, description="cost of raising a finding that is wrong")
    miss: float = Field(default=1.0, gt=0, description="cost of staying silent when the finding was right")

    @property
    def threshold(self) -> float:
        """Belief above which speaking costs less than silence."""
        return self.false_alarm / (self.false_alarm + self.miss)


@dataclass(frozen=True)
class Decision:
    belief: float
    threshold: float
    raise_finding: bool
    expected_cost_of_silence: float
    expected_cost_of_speaking: float

    @property
    def severity(self) -> float:
        """Rank findings by how much they matter, not by how sure the persona feels."""
        return self.expected_cost_of_silence


def decide(belief: float, loss: LossMatrix) -> Decision:
    silence = belief * loss.miss
    speaking = (1.0 - belief) * loss.false_alarm
    return Decision(
        belief=belief,
        threshold=loss.threshold,
        raise_finding=silence > speaking,
        expected_cost_of_silence=silence,
        expected_cost_of_speaking=speaking,
    )
