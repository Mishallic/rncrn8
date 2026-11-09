"""Documents and pitches: text or markdown, split on headings into citable sections."""

from __future__ import annotations

from pathlib import Path

from ..packet import TEXT_SUFFIXES, ReviewPacket, packet_from_text
from .base import AdapterError


def text_packet(text: str, title: str, kind: str = "idea") -> ReviewPacket:
    """Inline text, a pitch by default. Its target identity is its title."""
    packet = packet_from_text(text, title, kind)
    packet.metadata["target"] = title
    return packet


class DocumentAdapter:
    name = "document"
    kind = "document"

    def matches(self, target: Path) -> bool:
        return target.is_file() and (target.suffix.lower() in TEXT_SUFFIXES or target.suffix == "")

    def build(self, target: Path, **options) -> ReviewPacket:
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise AdapterError(f"{target} is not a text file") from exc
        packet = packet_from_text(text, options.get("title") or target.stem, self.kind)
        packet.metadata["target"] = target.name  # the file, whatever the title says this time
        return packet


class IdeaAdapter(DocumentAdapter):
    """A pitch is a document whose readers are investors, users and skeptics. Same split, kind 'idea'.
    It matches the same files as DocumentAdapter; ADAPTERS order makes it win only when --kind idea is given."""

    name = "idea"
    kind = "idea"
