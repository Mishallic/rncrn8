"""Target adapters: turn something to review into a ReviewPacket.

This is the only place that knows what a frame, a hunk or a subtitle is. The
core receives packets and never looks past them.
"""

from __future__ import annotations

from pathlib import Path

from ..packet import KINDS, ReviewPacket
from .base import Adapter, AdapterError
from .diff import DiffAdapter, git_diff_packet
from .document import DocumentAdapter, IdeaAdapter, text_packet
from .video import VideoAdapter

ADAPTERS: tuple[Adapter, ...] = (VideoAdapter(), DiffAdapter(), DocumentAdapter(), IdeaAdapter())
"""Order is the tie-break: when several adapters match a path and no kind is given, the first wins.
DocumentAdapter precedes IdeaAdapter on purpose, so a text file is a document unless --kind idea says otherwise."""


def detect(target: Path, kind: str | None = None) -> Adapter:
    """Pick the adapter for a path. An explicit kind wins when more than one would do."""
    target = Path(target)
    if not target.exists():
        raise FileNotFoundError(f"no such target: {target}")
    candidates = [a for a in ADAPTERS if a.matches(target)]
    if kind:
        for a in candidates:
            if a.kind == kind:
                return a
    if candidates:
        return candidates[0]
    if target.is_dir():
        raise AdapterError(
            f"{target} is a directory with no transcript or frame files; for code pass --diff <range>, for a video folder add transcript.srt or frames.json"
        )
    raise AdapterError(f"no adapter for '{target.suffix}'; pass a text file, a .patch, an .srt/.vtt, a video folder, or a packet JSON")


def build_packet(target: Path, kind: str | None = None, title: str | None = None, **options) -> ReviewPacket:
    """Load a packet JSON directly, or run the matching adapter."""
    target = Path(target)
    if target.is_file() and target.suffix.lower() == ".json":
        from ..packet import load_packet

        return load_packet(target, kind=kind, title=title)
    adapter = detect(target, kind)
    packet = adapter.build(target, **options)
    if kind and kind in KINDS:
        packet = packet.model_copy(update={"kind": kind})
    if title:
        packet = packet.model_copy(update={"title": title})
    return packet


__all__ = [
    "ADAPTERS",
    "Adapter",
    "AdapterError",
    "DiffAdapter",
    "DocumentAdapter",
    "IdeaAdapter",
    "VideoAdapter",
    "build_packet",
    "detect",
    "git_diff_packet",
    "text_packet",
]
