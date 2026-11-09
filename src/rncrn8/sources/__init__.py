"""Memory sources: ways to fill a persona's or the shared store, each with provenance.

Everything here is opt-in. Project and history read what is already on disk; notes and
the questionnaire take what a person says; research sends one persona to the web with a
budget. All of it lands as ordinary memories with source, origin, who and date set.
"""

from .extract import Fact, IngestResult, extract_facts, record_facts
from .history import ingest_history
from .notes import ingest_notes
from .project import ingest_project
from .questionnaire import QUESTIONS, record_answers
from .research import research

__all__ = [
    "Fact",
    "IngestResult",
    "QUESTIONS",
    "extract_facts",
    "ingest_history",
    "ingest_notes",
    "ingest_project",
    "record_answers",
    "record_facts",
    "research",
]
