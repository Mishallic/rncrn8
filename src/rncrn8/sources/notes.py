"""Notes ingest: a transcript, call notes or a message, attributed to the person who said it."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from ..config import SourceType
from ..memory import Workspace
from ..models import ModelAdapter
from .extract import EXTRACTION_RULES, Fact, IngestResult, extract_facts, record_facts


def paragraphs_as_facts(text: str, origin: str, who: str | None, when: date | None, limit: int) -> list[Fact]:
    """Fallback without a model: each paragraph is one claim, trimmed."""
    out: list[Fact] = []
    for para in re.split(r"\n\s*\n", text.strip()):
        clean = " ".join(para.split())
        if len(clean) < 15:
            continue
        out.append(Fact(claim=clean[:400], origin=origin, who=who, date=when, confidence=None, tags=[]))
        if len(out) >= limit:
            break
    return out


def ingest_notes(ws: Workspace, adapter: ModelAdapter, path: Path | None, who: str, origin: str, persona: str = "shared", when: date | None = None,
                 max_facts: int = 25, dry_run: bool = False, text: str | None = None) -> IngestResult:
    """Notes or a transcript, from a file or given as text, recorded as what `who` said."""
    if (path is None) == (text is None):
        raise ValueError("give the notes as a file or as text, one of the two")
    text = text if text is not None else Path(path).read_text(encoding="utf-8", errors="replace")
    result = IngestResult(source="human", persona=persona, extra={"chars": len(text)})
    system = (
        f"You read notes or a transcript of a conversation with {who} and extract what {who} actually said or decided, as separate facts. "
        "Attribute each fact to the speaker; keep hedges and disagreements as the speaker expressed them.\n\n" + EXTRACTION_RULES
    )
    user = f"NOTES (origin: {origin}; speaker: {who}{'; date: ' + when.isoformat() if when else ''})\n\n{text[:40000]}\n\nExtract at most {max_facts} facts. Use '{origin}' as origin and the speaker's name as who."
    facts, notes, error, _ = extract_facts(adapter, system, user, max_facts, label=f"ingest-notes:{persona}", default_origin=origin)
    result.notes, result.error = notes, error
    if not facts:
        facts = paragraphs_as_facts(text, origin, who, when, max_facts)
        result.notes = (result.notes + " " if result.notes else "") + "recorded paragraphs as claims (no model extraction)"
    for f in facts:
        f.who = f.who or who
        f.date = f.date or when
        f.origin = f.origin or origin
    if dry_run:
        result.extra["facts"] = [f.claim for f in facts]
        return result
    result.recorded, result.skipped_duplicates = record_facts(ws, persona, facts, SourceType.HUMAN, extra_tags=["ingest:notes"])
    return result
