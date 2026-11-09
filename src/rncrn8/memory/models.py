"""A memory entry: one claim, with where it came from and how far to trust it.

The provenance header is the whole point. A persona weighs "a paying customer
said this on a call" differently from "someone said this on Reddit in 2024"
differently from "I inferred this from the code". Without the tag the
internet drowns out the humans, because there is always more of it.
"""

from __future__ import annotations

import re
import secrets
import time
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from ..config import SourceType
from ..engine.beta import BetaCounts


def new_id(prefix: str = "m") -> str:
    """Time-sortable id: millisecond timestamp in hex plus six random bytes (collisions within one
    millisecond across threads were observed with two bytes)."""
    return f"{prefix}-{int(time.time() * 1000):011x}{secrets.token_hex(6)}"


def human_root(who: str, origin: str) -> str:
    """Dependence key for a human source: the same person in the same conversation is one source,
    however many things they said. It is an exact match on the normalised pair (lower case, whitespace
    collapsed): 'planning meeting' and 'planning meeting 2026-11-04' are two sources, and an explicit
    root says otherwise. `|` and `\\` are escaped so two different (who, origin) pairs cannot share a key."""

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip().lower().replace("\\", "\\\\").replace("|", "\\|")

    return f"{norm(who)}|{norm(origin)}"


class Stance(str, Enum):
    SUPPORTS = "supports"
    OPPOSES = "opposes"


class MemoryEntry(BaseModel):
    id: str = Field(default_factory=new_id)
    persona: str
    claim: str

    # --- provenance ---------------------------------------------------------------
    source: SourceType
    origin: str = Field(description="where exactly: a URL, 'call with team lead', 'git log', 'README.md'")
    who: str | None = Field(default=None, description="who said it, if anyone")
    date: _date = Field(default_factory=lambda: datetime.now(timezone.utc).date(), description="when the evidence was observed or published")
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    root: str | None = Field(default=None, description="root reference for dependence clustering: entries that trace to the same study, post or review share a root")

    # --- trust --------------------------------------------------------------------
    reliability: BetaCounts
    confirmed_by: list[str] = Field(default_factory=list)
    contradicted_by: list[str] = Field(default_factory=list)
    expires_at: _date | None = Field(default=None, description="past this date the entry is stale until re-checked")

    # --- organisation -------------------------------------------------------------
    tags: list[str] = Field(default_factory=list)
    note: str | None = None
    retired: str | None = Field(default=None, description="why this entry was retired; a retired entry stays in the file for the record and is never shown or cited again")

    @model_validator(mode="after")
    def _default_human_root(self) -> "MemoryEntry":
        # derived when an entry is read as well as when it is written, so human memories recorded
        # before this rule existed cluster too (they were stored with root null)
        if self.root is None and self.source == SourceType.HUMAN and self.who and self.origin:
            self.root = human_root(self.who, self.origin)
        return self

    @property
    def confidence(self) -> float:
        return self.reliability.mean

    def age_days(self, today: _date | None = None) -> float:
        today = today or datetime.now(timezone.utc).date()
        return float((today - self.date).days)

    def is_stale(self, today: _date | None = None) -> bool:
        if self.expires_at is None:
            return False
        today = today or datetime.now(timezone.utc).date()
        return today > self.expires_at

    @property
    def cluster_key(self) -> str:
        return self.root or self.id

    def refreshed(self, review_after_days: float | None, today: _date | None = None) -> "MemoryEntry":
        """Mark re-checked today: pushes the expiry out again."""
        today = today or datetime.now(timezone.utc).date()
        expires = today + timedelta(days=review_after_days) if review_after_days else None
        return self.model_copy(update={"expires_at": expires})
