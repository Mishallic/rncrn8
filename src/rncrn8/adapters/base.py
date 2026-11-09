from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..packet import ReviewPacket


class AdapterError(ValueError):
    """The target cannot be turned into a packet; the message says what to pass instead."""


class Adapter(Protocol):
    name: str
    kind: str

    def matches(self, target: Path) -> bool: ...

    def build(self, target: Path, **options) -> ReviewPacket: ...
