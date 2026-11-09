"""After a session, each persona rewrites its own summary file. Short, factual, three fixed headings."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..persona.models import Persona

if TYPE_CHECKING:  # pragma: no cover
    from .runner import FindingRecord, PersonaReviewRecord, ReviewRecord

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"summary_markdown": {"type": "string"}},
    "required": ["summary_markdown"],
    "additionalProperties": False,
}

HEADINGS = ("## What I tend to get right", "## What I was told to stop raising", "## What I am tracking")

SUMMARY_HISTORY_DIR = "summary-history"


def write_summary(ws: Any, persona: str, text: str, review_id: str) -> Path:
    """Replace summary.md under the persona's lock, keeping the previous text in summary-history/.

    A rewrite can drop what the persona had learned, and research on evolving contexts shows whole
    rewrites collapse; the history keeps every earlier version, so nothing a rewrite loses is gone.
    Two reviews finishing at once write one after the other instead of interleaving."""
    from datetime import datetime, timezone

    from ..memory.lock import locked

    d = ws.persona_dir(persona)
    path = d / "summary.md"
    with locked(d / "summary"):
        if path.exists():
            old = path.read_text(encoding="utf-8")
            if old.strip():
                hist = d / SUMMARY_HISTORY_DIR
                hist.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                (hist / f"{stamp}-before-{review_id}.md").write_text(old, encoding="utf-8")
        path.write_text(text, encoding="utf-8")
    return path


def build_summary_system(persona: Persona, max_words: int) -> str:
    return "\n".join(
        [
            f"You are {persona.display_name}. {persona.character.strip()}",
            "",
            "You keep a short summary of yourself that is loaded at the start of every future session. You are updating it now.",
            "Rules:",
            f"- Keep exactly these three headings, in this order: {', '.join(h[3:] for h in HEADINGS)}.",
            f"- At most {max_words} words in total. Drop old items before adding weak new ones.",
            "- Record only what happened: findings the panel agreed with, findings you conceded and why, questions left open. Never invent outcomes; the result of a finding is unknown until someone records it.",
            "- 'What I was told to stop raising' changes only when a person or the shared world said so, or when you conceded the same kind of point twice. A finding a person recorded as refuted belongs there, and leaves 'What I tend to get right'. An outcome marked unconfirmed never moves a finding there.",
            "- 'What I am tracking' lists things to re-check next time, each in one line.",
            "- Write in the first person, plainly, no praise of yourself.",
            "",
            "Respond with JSON: summary_markdown, the full new summary starting with a level-one heading of your name.",
        ]
    )


def build_summary_user(persona: Persona, current: str, review: "ReviewRecord", rec: "PersonaReviewRecord", given: list[tuple[str, "FindingRecord", str, float, str]]) -> str:
    """given: (owner, finding, stance, confidence, because) for the responses this persona gave to others."""
    lines = ["YOUR CURRENT SUMMARY:", current.strip() or "(empty)", "", f"WHAT HAPPENED IN REVIEW {review.id} ({review.kind}: {review.title}):", ""]
    lines.append("Your findings and how the panel took them:")
    if not rec.findings:
        lines.append("(none)")
    for f in rec.findings:
        resp = "; ".join(f"{r.persona} {r.stance} ({r.confidence:.2f})" + (f": {r.because}" if r.because else "") for r in f.responses) or "no responses"
        act = f.action if f.action != "hold" else "held"
        extra = f" because: {f.because}" if f.because and f.action != "hold" else ""
        lines.append(f"- [{f.fid}] {f.status.upper()} · you {act}{extra} · {f.claim}")
        lines.append(f"    panel: {resp}")
    lines.append("")
    lines.append("Responses you gave to others:")
    if not given:
        lines.append("(none)")
    for owner, f, stance, conf, because in given:
        lines.append(f"- {stance} ({conf:.2f}) with {owner} [{f.fid}] {f.claim}" + (f" because: {because}" if because else ""))
    if rec.prior_findings:
        lines += ["", "Your earlier findings on this target, as you judged them this time (a person's recorded outcome, where noted, is final):"]
        for pf in rec.prior_findings:
            note = f" ({pf.note})" if pf.note else ""
            lines.append(f"- {pf.status}{note}: {pf.claim or pf.memory_id}" + (f" ({pf.because})" if pf.because else ""))
    if rec.notes or rec.debate_notes:
        lines += ["", "Your own notes this session:", rec.notes, rec.debate_notes]
    lines += ["", "Write the new summary now."]
    return "\n".join(lines)


def normalise_summary(text: str, persona: Persona) -> str:
    text = text.strip()
    if not text.startswith("#"):
        text = f"# {persona.display_name}: summary\n\n{text}"
    return text + "\n"
