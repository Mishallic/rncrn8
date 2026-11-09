"""Markdown report for one review.

With a debate: agreed, disputed, unchallenged, held, dropped. Without one: raised and held.
Disagreement is left standing for the reader; nothing here averages it away.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .runner import FindingRecord, PersonaReviewRecord, ReviewRecord

_STANCE_VERB = {"agree": "agrees", "disagree": "disagrees", "unsure": "is unsure"}

SECTIONS = (
    ("agreed", "Agreed", "raised, backed by at least one other reviewer, nobody disagreeing"),
    ("disputed", "Disputed", "another reviewer disagreed; the disagreement is left standing for you"),
    ("unchallenged", "Unchallenged", "raised, no other reviewer took a position"),
    ("held", "Held", "below the owner's threshold after all evidence"),
    ("dropped", "Dropped", "conceded by the reviewer who raised it"),
)


def _evidence_line(f: "FindingRecord") -> str:
    parts = []
    for c in f.contributions:
        label = c.get("label", c["ref"])
        if c.get("role") == "reading" and c["contribution"] != 0 and (c["contribution"] > 0) != bool(c["supports"]):
            label += ", counts against" if c["supports"] else ", counts for"  # calibrated below one half: wrong more often than right at this confidence
        stated = c.get("stated")
        shown = f"{c['reliability']:.2f}" if stated is None or abs(stated - c["reliability"]) < 0.005 else f"{stated:.2f} stated, {c['reliability']:.2f} calibrated"
        scale = f", panel weight x{c['scale']:.2f}" if abs(c.get("scale", 1.0) - 1.0) >= 0.005 else ""
        parts.append(f"{label} {shown} ({c['contribution']:+.2f}{scale})")
    return ", ".join(parts) or "none"


def _finding_block(i: int | None, p: "PersonaReviewRecord", f: "FindingRecord", debated: bool) -> list[str]:
    head = f"{i}. " if i is not None else "- "
    lines = [f"{head}[{f.fid}] {f.where} · {f.kind}: {f.claim}"]
    belief = f"belief {f.belief:.2f}" + (f" (round 1: {f.belief_round1:.2f})" if debated and abs(f.belief - f.belief_round1) >= 0.005 else "")
    lines.append(f"   {belief} vs threshold {f.threshold:.2f} · severity {f.severity:.2f}" + ("  · CONFLICT in evidence" if f.conflict else ""))
    if f.round > 1:
        lines.append(f"   added in the debate" + (f", prompted by {', '.join(f.prompted_by)}" if f.prompted_by else ""))
    if f.rationale:
        lines.append(f"   why: {f.rationale}")
    if f.fix:
        lines.append(f"   fix: {f.fix}")
    lines.append(f"   evidence: {_evidence_line(f)}")
    if f.folded_past_readings:
        folded = ", ".join(f"{name} {n}" for name, n in f.folded_past_readings.items())
        lines.append(f"   past readings cited and counted once through the current reading: {folded}")
    if f.dropped_refs:
        lines.append(f"   dropped: cited ids that do not exist: {', '.join(f.dropped_refs)}")
    if f.decision_id:
        lines.append(f"   decision: {f.decision_id}  (record what happened with `rncrn8 outcome {f.decision_id} --confirmed|--refuted`)")
    if f.merged_from:
        lines.append(f"   also raised as: {', '.join(f.merged_from)} (merged into this one)")
    for r in f.responses:
        extra = f": {r.because}" if r.because else ""
        lines.append(f"   {r.persona} {_STANCE_VERB.get(r.stance, r.stance)} ({r.confidence:.2f}){extra}")
        if r.objection:
            lines.append(f"      strongest objection: {r.objection}")
        if r.dropped_refs:
            lines.append(f"      dropped: cited ids that do not exist: {', '.join(r.dropped_refs)}")
    if f.action == "revise":
        lines.append(f"   {p.persona} revised ({f.final_confidence:.2f}) because: {f.because}")
        if f.original_claim:
            lines.append(f'      was: "{f.original_claim}"')
    elif f.action == "concede":
        lines.append(f"   {p.persona} conceded because: {f.because}" + (f" (changed by {', '.join(f.changed_by)})" if f.changed_by else ""))
    elif debated and f.final_confidence != f.stated_confidence:
        lines.append(f"   {p.persona} holds at {f.final_confidence:.2f} (was {f.stated_confidence:.2f})" + (f": {f.because}" if f.because else ""))
    return lines


def render_report(review: "ReviewRecord") -> str:
    lines: list[str] = []
    lines.append(f"# Review {review.id}: {review.title}")
    panel = ", ".join(p.persona for p in review.personas)
    models = sorted({p.model for p in review.personas if p.model})
    stamp = review.created_at.strftime("%Y-%m-%d %H:%M UTC")
    rounds = "review + debate" if review.debated else "review only"
    if review.summaries_updated:
        rounds += " + summaries updated"
    lines.append(f"kind: {review.kind} · panel: {panel} · model: {', '.join(models) or review.adapter} · {rounds} · {stamp}")
    if review.dry_run:
        lines.append("DRY RUN: no model was called and nothing was recorded.")
    for w in review.warnings:
        lines.append(f"note: {w}")
    failed = review.failed()
    if review.all_failed():
        lines.append(f"REVIEW FAILED: no persona could run, so nothing was reviewed. The first error: {failed[0].error}")
    elif failed:
        lines.append(f"note: {len(failed)} of {len(review.personas)} personas failed and are missing from this review "
                     f"({', '.join(p.persona for p in failed)}); see Panel health")
    debate_failed = [p for p in review.personas if p.debate_error]
    if debate_failed and not review.all_failed():
        lines.append(f"note: the debate round failed for {len(debate_failed)} of {len(review.personas)} personas "
                     f"({', '.join(p.persona for p in debate_failed)}), so their findings were not challenged or backed by them; see Panel health")
    lines.append("")

    if review.debated:
        for status, title, blurb in SECTIONS:
            items = review.by_status(status)
            lines.append(f"## {title} ({len(items)})")
            lines.append(f"_{blurb}_")
            if not items:
                lines.append("None.")
            for i, (p, f) in enumerate(items, start=1):
                lines += _finding_block(i if status != "held" and status != "dropped" else None, p, f, True)
            lines.append("")
        merged = review.by_status("merged")
        if merged:
            lines.append(f"## Merged ({len(merged)})")
            lines.append("_the same point raised by another reviewer; folded in as agreement_")
            for p, f in merged:
                lines.append(f"- [{f.fid}] -> [{f.merged_into}]: {f.claim}")
            lines.append("")
    else:
        raised = review.raised()
        lines.append(f"## Raised ({len(raised)})")
        if not raised:
            lines.append("None: every persona failed." if review.all_failed() else "Nothing crossed a persona's threshold.")
        for i, (p, f) in enumerate(raised, start=1):
            lines += _finding_block(i, p, f, False)
        lines.append("")
        held = review.held()
        lines.append(f"## Held ({len(held)})")
        if not held:
            lines.append("None.")
        for p, f in held:
            lines.append(f"- [{f.fid}] {f.where} · {f.kind}: {f.claim}  (belief {f.belief:.2f} < {f.threshold:.2f})")
        lines.append("")

    lines.append("## What each persona understood")
    for p in review.personas:
        if p.error:
            lines.append(f"- {p.persona}: ERROR {p.error}")
        elif p.refused:
            lines.append(f"- {p.persona}: the model declined this request ({p.refusal})")
        else:
            lines.append(f"- {p.persona}: {p.understood or '(nothing returned)'}")
    lines.append("")

    follow = [(p.persona, pf) for p in review.personas for pf in p.prior_findings]
    if follow:
        lines.append("## Follow-ups on earlier findings")
        lines.append("_each persona checked its own earlier findings on this target; addressed and wrong become outcomes_")
        for name, pf in follow:
            verdict = {"addressed": "confirmed", "wrong": "refuted"}.get(pf.status)
            tail = ""
            if pf.recorded and pf.decision_id:
                tail = f" -> {verdict} {pf.decision_id}"
            elif pf.note:
                tail = f" ({pf.note})"
            lines.append(f"- {name}: {pf.memory_id} {pf.status}{tail}" + (f": {pf.because}" if pf.because else ""))
        lines.append("")

    # a dry run calls no model, so the panel has said nothing and made no calls worth counting
    notes = [] if review.dry_run else [(p.persona, n) for p in review.personas for n in (p.notes, p.debate_notes) if n]
    if notes:
        lines.append("## Notes from the panel")
        for name, note in notes:
            lines.append(f"- {name}: {note}")
        lines.append("")

    lines.append("## Panel health")
    for p in review.personas:
        shown = f"{p.memories_shown} of {p.memories_available} memories shown" if p.memories_available else f"{p.memories_shown} memories shown"
        bits = [f"{len(p.findings)} {'finding' if len(p.findings) == 1 else 'findings'}", shown]
        if not review.dry_run:
            bits.append(f"{p.calls} {'call' if p.calls == 1 else 'calls'}")
        bits.append(f"calibration on {p.calibration_points} {'outcome' if p.calibration_points == 1 else 'outcomes'}" if p.calibration_points else "uncalibrated (no outcomes yet)")
        if p.panel_weight is not None:
            bits.append(f"panel weight {p.panel_weight:.2f}")
        if p.malformed:
            bits.append(f"{p.malformed} malformed dropped")
        dropped = sum(len(f.dropped_refs) for f in p.findings) + sum(len(r.dropped_refs) for q in review.personas for f in q.findings for r in f.responses if r.persona == p.persona)
        if dropped:
            bits.append(f"{dropped} invented memory ids dropped")
        if p.debate_error:
            bits.append(f"debate failed: {p.debate_error}")
        if p.summary_updated:
            bits.append("summary updated")
        elif p.summary_error:
            bits.append(f"summary not updated: {p.summary_error}")
        if p.input_tokens is not None:
            cache = f" / cache wrote {p.cache_write_tokens or 0} read {p.cache_read_tokens or 0}"
            bits.append(f"tokens in {p.input_tokens} / out {p.output_tokens}{cache}")
        if p.call_log and not review.dry_run:
            bits.append("calls: " + "; ".join(f"{c['stage']} in {c['input'] or 0} read {c['cache_read'] or 0} wrote {c['cache_write'] or 0}" for c in p.call_log))
        lines.append(f"- {p.persona}: " + ", ".join(bits))
    return "\n".join(lines) + "\n"
