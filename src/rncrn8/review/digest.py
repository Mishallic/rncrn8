"""A review as a structured digest, for the assistant that stands between the panel and the user.

The markdown report is for reading in full. This is what an assistant needs to put the review in plain
words and to record what the user says back: each finding's claim, where it is, the fix, the other
personas who backed or disputed it in their own words, and the decision id that `answer` and `outcome`
take. Findings come in the report's order, what needs the user first; the numbers stay available for a
user who asks.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .runner import FindingRecord, PersonaReviewRecord, ReviewRecord

RAISED = ("agreed", "disputed", "unchallenged")
ORDER = (*RAISED, "held", "dropped")


def _voices(f: "FindingRecord", stance: str) -> list[dict[str, str]]:
    return [{"who": r.persona, "says": r.because} for r in f.responses if r.stance == stance]


def _finding(p: "PersonaReviewRecord", f: "FindingRecord", status: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "decision_id": f.decision_id,
        "finding": f.fid,
        "status": status,
        "kind": f.kind,
        "raised_by": p.display_name,
        "where": f.where,
        "claim": f.claim,
        "why": f.rationale,
        "fix": f.fix,
        "agree": _voices(f, "agree"),
        "disagree": _voices(f, "disagree"),
        "unsure": _voices(f, "unsure"),
        "revised_from": f.original_claim if f.action == "revise" else None,
        "conceded_because": f.because if f.action == "concede" else None,
        "also_raised_by": f.merged_from,
        "added_in_debate": f.round > 1 or None,
        "belief": round(f.belief, 2),
        "threshold": round(f.threshold, 2),
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def digest(review: "ReviewRecord", review_dir: Path | None = None) -> dict[str, Any]:
    if review.debated:
        items = [(p, f, s) for s in ORDER for p, f in review.by_status(s)]
        counts = {s: len(review.by_status(s)) for s in (*ORDER, "merged")}
        merged = [{"finding": f.fid, "raised_by": p.display_name, "claim": f.claim, "folded_into": f.merged_into} for p, f in review.by_status("merged")]
    else:
        items = [(p, f, "raised") for p, f in review.raised()] + [(p, f, "held") for p, f in review.held()]
        counts = {"raised": len(review.raised()), "held": len(review.held())}
        merged = []
    out: dict[str, Any] = {
        "review_id": review.id,
        "title": review.title,
        "target": review.target,
        "kind": review.kind,
        "panel": {p.persona: p.display_name for p in review.personas},
        "debated": review.debated,
        "dry_run": review.dry_run,
        "counts": counts,
        "findings": [_finding(p, f, s) for p, f, s in items],
        "merged": merged,
        "earlier_findings": [
            {"who": p.persona, "memory_id": pf.memory_id, "now": pf.status, "because": pf.because, "recorded_as": pf.decision_id if pf.recorded else None}
            for p in review.personas for pf in p.prior_findings
        ],
        "panel_notes": [] if review.dry_run else [{"who": p.persona, "says": n} for p in review.personas for n in (p.notes, p.debate_notes) if n],
        "problems": [f"{p.persona}: {p.error}" for p in review.personas if p.error]
        + [f"{p.persona}: the model declined ({p.refusal})" for p in review.personas if p.refused]
        + [f"{p.persona}: debate failed: {p.debate_error}" for p in review.personas if p.debate_error],
        "notes": list(review.warnings),
    }
    if review.all_failed():
        out["review_failed"] = "no persona could run, so nothing was reviewed; tell the user the first problem listed"
    if review_dir is not None:
        out["report"] = str(review_dir / "report.md")
    return {k: v for k, v in out.items() if v not in (None, [], "") or k in ("findings", "counts")}
