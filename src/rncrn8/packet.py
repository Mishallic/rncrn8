"""Review packets: the one shape every target becomes before a persona sees it.

Nothing downstream knows what the target was. A video becomes descriptions and
speech with times; a code change becomes its changed regions with file and line
refs; a document becomes its sections. The adapters package builds packets. This
module only defines them and loads the two generic forms: a plain text or
markdown file, and a packet JSON file produced by any external script.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

KINDS = ("document", "code", "video", "idea", "other")
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".text"}

GIT_PROVENANCE_KEYS = ("committed", "reviewed", "base", "head", "repo", "spec")
"""Metadata only the git adapter may set. A packet loaded from JSON cannot claim it, so a
follow-up never runs git on values a script supplied."""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "untitled"


class Section(BaseModel):
    ref: str = Field(description="stable locator a persona can cite: '§2', 't=00:12', 'src/a.py:40'")
    title: str | None = None
    text: str
    context: bool = Field(default=False, description="not under review: shown so the rest can be judged, such as unchanged code that uses what a change modifies; not counted toward review.max_packet_chars")


class ReviewPacket(BaseModel):
    kind: str = "document"
    title: str
    summary: str | None = None
    sections: list[Section]
    metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n\n".join(s.text for s in self.sections)

    @property
    def char_count(self) -> int:
        return sum(len(s.text) for s in self.sections)

    @property
    def reviewed_chars(self) -> int:
        """Characters of what is under review, which the size limit applies to: context sections have a budget of their own."""
        return sum(len(s.text) for s in self.sections if not s.context)

    def refs(self) -> list[str]:
        return [s.ref for s in self.sections]

    @property
    def target_tag(self) -> str:
        """Stable identity of what was reviewed, for memory tags: the adapter's `target` metadata
        (a repo name, a file name) when present, else the title. Must not change with a diff range."""
        return "target:" + slug(self.metadata.get("target") or self.title)

    def render(self) -> str:
        lines = [f"TITLE: {self.title}", f"KIND: {self.kind}"]
        if self.summary:
            lines.append(f"SUMMARY: {self.summary}")
        for k, v in sorted(self.metadata.items()):
            lines.append(f"{k.upper()}: {v}")
        lines.append("")
        for s in self.sections:
            head = f"[{s.ref}]" + (f" {s.title}" if s.title else "")
            lines.append(head)
            lines.append(s.text.rstrip())
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for para in re.split(r"\n\s*\n", text):
        if size + len(para) > max_chars and current:
            parts.append("\n\n".join(current))
            current, size = [], 0
        current.append(para)
        size += len(para) + 2
    if current:
        parts.append("\n\n".join(current))
    return parts


def packet_from_text(text: str, title: str, kind: str = "document", max_section_chars: int = 3000) -> ReviewPacket:
    """Split on markdown headings; without headings, on blank lines into bounded chunks."""
    blocks: list[tuple[str | None, str]] = []
    current_title: str | None = None
    buf: list[str] = []
    saw_heading = False
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.lstrip()
        if fence is None and (stripped.startswith("```") or stripped.startswith("~~~")):
            fence = stripped[:3]
        elif fence is not None and stripped.startswith(fence):
            fence = None
        m = None if fence is not None else _HEADING.match(line)
        if m:
            saw_heading = True
            if buf and "".join(buf).strip():
                blocks.append((current_title, "\n".join(buf).strip()))
            elif current_title is not None:
                blocks.append((current_title, ""))
            current_title = m.group(2)
            buf = []
        else:
            buf.append(line)
    if buf and "".join(buf).strip():
        blocks.append((current_title, "\n".join(buf).strip()))
    elif current_title is not None and (not blocks or blocks[-1][0] != current_title):
        blocks.append((current_title, ""))

    if not saw_heading:
        blocks = [(None, chunk) for chunk in _split_long(text.strip(), max_section_chars)]

    sections: list[Section] = []
    n = 0
    for sec_title, body in blocks:
        n += 1
        pieces = _split_long(body, max_section_chars) if body else [""]
        if len(pieces) == 1:
            sections.append(Section(ref=f"§{n}", title=sec_title, text=pieces[0]))
        else:
            for i, piece in enumerate(pieces, start=1):
                sections.append(Section(ref=f"§{n}.{i}", title=sec_title if i == 1 else None, text=piece))
    if not sections:
        sections.append(Section(ref="§1", title=None, text=""))
    return ReviewPacket(kind=kind, title=title, sections=sections)


def load_packet(path: Path, kind: str | None = None, title: str | None = None) -> ReviewPacket:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no such target: {path}")
    if path.is_dir():
        raise ValueError("directories need a target adapter (deliverable 5); pass a file or a packet JSON")
    if path.suffix.lower() == ".json":
        packet = ReviewPacket.model_validate_json(path.read_text(encoding="utf-8"))
        # a script's packet is data: it may describe anything, but it may not claim git provenance
        packet = packet.model_copy(update={"metadata": {k: v for k, v in packet.metadata.items() if k not in GIT_PROVENANCE_KEYS}})
        if kind:
            packet = packet.model_copy(update={"kind": kind})
        if title:
            packet = packet.model_copy(update={"title": title})
        return packet
    if path.suffix.lower() in TEXT_SUFFIXES or path.suffix == "":
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{path} is not a text file; pass a packet JSON built by an adapter") from exc
        return packet_from_text(text, title or path.stem, kind or "document")
    raise ValueError(f"no built-in loader for '{path.suffix}' yet; pass a text file or a packet JSON")


def size_by_path(packet: "ReviewPacket") -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Characters per folder (one and two levels deep) and per file, largest first: what a target too large to
    review in one go is made of, so it can be split into parts that each fit."""
    files: dict[str, int] = {}
    for s in packet.sections:
        if s.context:
            continue
        path = s.ref.rsplit(":", 1)[0] if re.fullmatch(r".+:\d+(-\d+)?", s.ref) else s.ref
        files[path] = files.get(path, 0) + len(s.text)
    folders: dict[str, int] = {}
    for path, n in files.items():
        parts = path.split("/")[:-1]
        for depth in (1, 2):
            if len(parts) >= depth:
                key = "/".join(parts[:depth]) + "/"
                folders[key] = folders.get(key, 0) + n
    largest = lambda d: sorted(d.items(), key=lambda kv: kv[1], reverse=True)  # noqa: E731
    return largest(folders), largest(files)
