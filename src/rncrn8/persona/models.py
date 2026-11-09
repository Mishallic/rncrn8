"""A persona is an identity plus a way of weighing evidence.

Two personas reading the same memory store reach different conclusions on
purpose: the trust profile says which sources this kind of expert believes,
and the loss matrix says what kind of mistake it fears more.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from ..config import EngineConfig, SourcePrior, SourceType
from ..engine.beta import BetaCounts
from ..engine.decision import LossMatrix

_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")


class Persona(BaseModel):
    name: str = Field(description="slug used in paths and commands")
    display_name: str
    character: str = Field(description="who this persona is and how it reads things")
    rubric: list[str] = Field(default_factory=list, description="what it checks, in order")
    applies_to: list[str] = Field(default_factory=lambda: ["*"], description="target kinds this persona reviews; '*' for all")
    model: str | None = Field(default=None, description="preferred model id; the adapter decides what that means")
    trust_profile: dict[SourceType, BetaCounts] = Field(default_factory=dict, description="prior reliability overrides per source class")
    loss: LossMatrix | None = Field(default=None, description="costs of a false alarm and a miss; falls back to config default")

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG.match(v):
            raise ValueError("persona name must be a slug: lowercase letters, digits, '-' or '_', starting with a letter")
        return v

    def prior_for(self, source: SourceType, config: EngineConfig) -> SourcePrior:
        base = config.prior_for(source)
        override = self.trust_profile.get(source)
        if override is None:
            return base
        return base.model_copy(update={"a": override.a, "b": override.b})

    def loss_for(self, config: EngineConfig) -> LossMatrix:
        return self.loss or config.default_loss

    def reviews(self, target_kind: str) -> bool:
        return "*" in self.applies_to or target_kind in self.applies_to
