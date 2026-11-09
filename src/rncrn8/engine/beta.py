"""Beta-distributed reliability for a witness, an origin, or a source class.

A reliability is "the probability this witness is right when it speaks".
We keep it as two pseudo-counts rather than a single number so that the
engine knows not only how reliable a source is but how sure it is of that.
Priors are the persona's trust profile; outcomes move the counts.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

_EPS = 1e-6


class BetaCounts(BaseModel):
    """Pseudo-counts of confirmations (a) and contradictions (b)."""

    model_config = {"frozen": True}

    a: float = Field(gt=0, description="confirmed pseudo-count")
    b: float = Field(gt=0, description="contradicted pseudo-count")

    @property
    def mean(self) -> float:
        return self.a / (self.a + self.b)

    @property
    def strength(self) -> float:
        """Total pseudo-count: how much evidence stands behind the mean."""
        return self.a + self.b

    @property
    def variance(self) -> float:
        n = self.a + self.b
        return (self.a * self.b) / (n * n * (n + 1.0))

    def confirmed(self, amount: float = 1.0) -> "BetaCounts":
        return BetaCounts(a=self.a + amount, b=self.b)

    def contradicted(self, amount: float = 1.0) -> "BetaCounts":
        return BetaCounts(a=self.a, b=self.b + amount)

    @classmethod
    def from_mean(cls, mean: float, strength: float) -> "BetaCounts":
        """Build counts with a given mean while keeping a given total strength."""
        if strength <= 0:
            raise ValueError("strength must be positive")
        m = min(max(mean, _EPS), 1.0 - _EPS)
        return cls(a=m * strength, b=(1.0 - m) * strength)

    def recentered(self, mean: float) -> "BetaCounts":
        """Same strength, new mean. Used when a recorder states a confidence."""
        return BetaCounts.from_mean(mean, self.strength)
