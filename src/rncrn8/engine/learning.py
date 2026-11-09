"""Attribute an outcome back to the witnesses that produced the belief.

Each witness that contributed gets its Beta counts nudged in proportion to
its share of the total evidence. Witnesses that pointed the right way gain
confirmations; the rest gain contradictions. The learning rate scales how
much one outcome can move a source.
"""

from __future__ import annotations

from dataclasses import dataclass

from .evidence import Evidence


@dataclass(frozen=True)
class WitnessUpdate:
    ref: str
    share: float
    agreed: bool
    amount: float


def attribute_outcome(evidence: Evidence, claim_true: bool, learning_rate: float = 1.0) -> list[WitnessUpdate]:
    live = [c for c in evidence.contributions if c.contribution != 0.0]
    total = sum(abs(c.contribution) for c in live)
    if total <= 0.0:
        return []
    updates: list[WitnessUpdate] = []
    for c in live:
        share = abs(c.contribution) / total
        agreed = c.supports == claim_true
        updates.append(WitnessUpdate(ref=c.ref, share=share, agreed=agreed, amount=learning_rate * share))
    return updates
