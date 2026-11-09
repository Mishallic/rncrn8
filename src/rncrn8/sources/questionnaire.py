"""Five questions on first run. Two minutes of a person's time, recorded as human memories."""

from __future__ import annotations

from ..config import SourceType
from ..memory import MemoryEntry, Workspace
from ..service import remember

QUESTIONS: tuple[tuple[str, str, list[str]], ...] = (
    ("project", "What is this project, in one sentence, and who is it for?", ["project"]),
    ("priority", "What matters most right now?", ["priority"]),
    ("stop", "What are you tired of hearing from reviewers? Personas will stop raising it.", ["stop-raising"]),
    ("broke", "What broke or went wrong recently?", ["history"]),
    ("constraints", "What cannot change: deadlines, platforms, budget, people?", ["constraint"]),
)


def record_answers(ws: Workspace, answers: dict[str, str], who: str = "the user", persona: str = "shared") -> list[MemoryEntry]:
    entries: list[MemoryEntry] = []
    for key, question, tags in QUESTIONS:
        answer = (answers.get(key) or "").strip()
        if not answer:
            continue
        entries.append(
            remember(ws, persona, answer[:600], SourceType.HUMAN, origin="questionnaire", who=who, tags=["ingest:questionnaire", *tags], note=question)
        )
    return entries
