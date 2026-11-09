"""Research: one persona searches the web through its own lens, with a budget, and remembers what it found."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..config import SourceType
from ..memory import MemoryEntry, Workspace
from ..models import ModelAdapter
from ..persona import load_persona
from ..service import remember
from .extract import EXTRACTION_RULES, Fact, extract_facts, record_facts

WEB_SEARCH_TOOL_TYPE = "web_search_20260209"

UNSEEN_URL_TAG = "url-not-in-search-results"
UNSEEN_URL_CONFIDENCE = 0.3


def web_search_tool(budget: int) -> dict[str, Any]:
    return {"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": max(1, int(budget))}


@dataclass
class ResearchResult:
    persona: str
    question: str
    recorded: list[MemoryEntry] = field(default_factory=list)
    skipped_duplicates: int = 0
    notes: str = ""
    error: str | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    notes_entry: MemoryEntry | None = None


def _root(url: str) -> str | None:
    """The page, not the site: two articles on one host are independent sources; comments in one
    thread are not. Query strings and fragments are dropped so one article has one root."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = parsed.netloc.lower().removeprefix("www.")
    if not host:
        return None
    path = parsed.path.rstrip("/")
    query = f"?{parsed.query}" if host.endswith("ycombinator.com") and parsed.query else ""  # HN threads live in the query
    return f"{host}{path}{query}"


def research(ws: Workspace, adapter: ModelAdapter, persona: str, question: str, budget: int = 5, max_facts: int = 12, shared: bool = False, dry_run: bool = False) -> ResearchResult:
    p = load_persona(ws, persona)
    target = "shared" if shared else persona
    result = ResearchResult(persona=persona, question=question)
    system = (
        f"You are {p.display_name}. {p.character.strip()}\n\n"
        "You are researching a question on the web through your own lens, for a project you review. Search, read, and then report facts, "
        "each with the URL it came from, who wrote or published it, and the date if the page gives one. Prefer primary sources, studies, "
        "official pages and first-hand accounts over commentary. Note vendor claims as vendor claims. Say in notes what you could not find.\n\n" + EXTRACTION_RULES
    )
    user = f"QUESTION: {question}\n\nYou may search at most {budget} times. Then respond with JSON matching the schema: facts (origin must be the URL), notes."
    if dry_run:
        result.notes = "dry run: no search made"
        return result
    facts, notes, error, sources = extract_facts(adapter, system, user, max_facts, label=f"research:{persona}", tools=[web_search_tool(budget)])
    result.notes, result.error, result.sources = notes, error, sources
    seen = {r for r in (_root(str(s.get("url") or "")) for s in sources) if r}
    for f in facts:
        f.root = _root(f.origin)
        extra = ["research"] + (["date:unknown"] if f.date is None else [])
        if f.root not in seen:
            # the model cited a page the search never returned: it may be real, or it may be invented, so the
            # fact is kept as a weak lead, tagged, and never stronger than UNSEEN_URL_CONFIDENCE
            extra.append(UNSEEN_URL_TAG)
            f.confidence = min(f.confidence if f.confidence is not None else UNSEEN_URL_CONFIDENCE, UNSEEN_URL_CONFIDENCE)
        f.tags = sorted(set(f.tags + extra))
    if facts and not seen:
        result.notes = (result.notes + "\n" if result.notes else "") + "No search results came back, so no fact's source could be checked; all are kept as weak leads."
    result.facts = facts
    result.recorded, result.skipped_duplicates = record_facts(ws, target, facts, SourceType.INTERNET, extra_tags=["ingest:research"])
    if notes.strip() and error is None:
        # the persona's own synthesis is a memory too: what it concluded and what it could not find
        summary = " ".join(notes.split())
        result.notes_entry = remember(
            ws,
            persona,
            f"Researched '{question[:160]}': {summary[:420]}" + ("…" if len(summary) > 420 else ""),
            SourceType.INFERENCE,
            origin=f"inference:{persona}",
            tags=["research", "research-notes"],
            note=summary[:4000],
        )
    return result
