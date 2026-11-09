"""The debate round: each persona reads the others and must answer them, in its own call.

Anti-collapse rules live in the prompt: a change of position must name what changed
it, agreement needs a reason of one's own, and nobody is asked for a consensus.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel, Field, ValidationError

from ..packet import ReviewPacket

if TYPE_CHECKING:  # pragma: no cover
    from .runner import FindingRecord

ACTIONS = ("hold", "revise", "concede", "merge")
STANCES = ("agree", "disagree", "unsure")

DEBATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "positions": {
            "type": "array",
            "description": "one entry per finding of your own",
            "items": {
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string"},
                    "action": {"type": "string", "enum": list(ACTIONS)},
                    "confidence": {"type": "number", "description": "your confidence now, 0 to 1"},
                    "because": {"type": "string"},
                    "changed_by": {"type": "array", "items": {"type": "string"}, "description": "finding ids or your memory ids that changed your position; required unless you hold"},
                    "revised_claim": {"type": "string", "description": "the new wording if you revise; empty otherwise"},
                    "duplicate_of": {"type": "string", "description": "with action merge: the other reviewer's finding id that makes the same point; empty otherwise"},
                },
                "required": ["finding_id", "action", "confidence", "because", "changed_by", "revised_claim", "duplicate_of"],
                "additionalProperties": False,
            },
        },
        "responses": {
            "type": "array",
            "description": "one entry per finding by another reviewer",
            "items": {
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string"},
                    "stance": {"type": "string", "enum": list(STANCES)},
                    "confidence": {"type": "number", "description": "0 to 1: how sure you are of your stance"},
                    "because": {"type": "string", "description": "a reason from your own reading or memory; 'they said so' is not one"},
                    "objection": {"type": "string", "description": "the strongest case against this finding you can make from the target or your memory, even if you agree; what would have to be true for it to be wrong. 'none' only if you can think of nothing"},
                    "supports": {"type": "array", "items": {"type": "string"}, "description": "your memory ids that support their claim"},
                    "opposes": {"type": "array", "items": {"type": "string"}, "description": "your memory ids that oppose their claim"},
                },
                "required": ["finding_id", "stance", "confidence", "because", "objection", "supports", "opposes"],
                "additionalProperties": False,
            },
        },
        "new_findings": {
            "type": "array",
            "description": "only what another reviewer's point revealed that you missed",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["problem", "risk", "question", "strength"]},
                    "claim": {"type": "string"},
                    "where": {"type": "string", "description": "the section ref; where the target shows line numbers, the exact lines as path:start-end"},
                    "rationale": {"type": "string"},
                    "fix": {"type": "string"},
                    "confidence": {"type": "number"},
                    "supports": {"type": "array", "items": {"type": "string"}},
                    "opposes": {"type": "array", "items": {"type": "string"}},
                    "prompted_by": {"type": "array", "items": {"type": "string"}, "description": "the other reviewers' finding ids that prompted this"},
                },
                "required": ["kind", "claim", "where", "rationale", "fix", "confidence", "supports", "opposes", "prompted_by"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["positions", "responses", "new_findings", "notes"],
    "additionalProperties": False,
}


class RawPosition(BaseModel):
    finding_id: str
    action: str = "hold"
    confidence: float | None = None
    because: str = ""
    changed_by: list[str] = Field(default_factory=list)
    revised_claim: str = ""
    duplicate_of: str = ""


class RawResponse(BaseModel):
    finding_id: str
    stance: str = "unsure"
    confidence: float = 0.5
    because: str = ""
    objection: str = ""
    supports: list[str] = Field(default_factory=list)
    opposes: list[str] = Field(default_factory=list)


class DebateOutput(BaseModel):
    positions: list[RawPosition] = Field(default_factory=list)
    responses: list[RawResponse] = Field(default_factory=list)
    new_findings: list[dict[str, Any]] = Field(default_factory=list)
    notes: str = ""
    malformed: int = 0


def parse_debate(data: Any) -> DebateOutput:
    out = DebateOutput()
    if not isinstance(data, dict):
        out.malformed += 1
        return out
    for item in data.get("positions") or []:
        try:
            p = RawPosition.model_validate(item)
        except ValidationError:
            out.malformed += 1
            continue
        if p.action not in ACTIONS:
            p.action = "hold"
        if p.confidence is not None and not (isinstance(p.confidence, (int, float)) and p.confidence == p.confidence and abs(p.confidence) != float("inf")):
            p.confidence = None
        out.positions.append(p)
    for item in data.get("responses") or []:
        try:
            r = RawResponse.model_validate(item)
        except ValidationError:
            out.malformed += 1
            continue
        if r.stance not in STANCES:
            r.stance = "unsure"
        r.supports = [s for s in dict.fromkeys(r.supports) if isinstance(s, str)]
        r.opposes = [s for s in dict.fromkeys(r.opposes) if isinstance(s, str) and s not in r.supports]
        out.responses.append(r)
    nf = data.get("new_findings") or []
    out.new_findings = [x for x in nf if isinstance(x, dict)] if isinstance(nf, list) else []
    out.notes = str(data.get("notes", ""))[:4000]
    return out


def _clip(text: str, n: int = 320) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def build_debate_prompt(
    packet: ReviewPacket,
    me: str,
    mine: list["FindingRecord"],
    others: list[tuple[str, "FindingRecord"]],
    describe_evidence: Callable[[str, "FindingRecord"], list[str]],
) -> str:
    """describe_evidence(owner, finding) returns provenance lines for the finding's cited memories."""
    lines = ["DEBATE ROUND (the target is still the one under REVIEW TARGET above)", ""]
    lines.append("Your findings from round 1 (reference them by the id in brackets, exactly as written):")
    if not mine:
        lines.append("(you raised none)")
    for f in mine:
        verdict = "RAISED" if f.raise_finding else "HELD below your threshold"
        lines.append(f"[{f.fid}] {f.where} · {f.kind} · your confidence {f.stated_confidence:.2f} · belief after evidence {f.belief:.2f} · {verdict}")
        lines.append(f"    {f.claim}")
        if f.rationale:
            lines.append(f"    why: {_clip(f.rationale)}")
        ev = describe_evidence(me, f)
        lines.append("    your evidence: " + ("; ".join(ev) if ev else "your reading only"))
    lines.append("")
    lines.append("Other reviewers' findings:")
    if not others:
        lines.append("(none)")
    for owner, f in others:
        verdict = "raised" if f.raise_finding else "held"
        lines.append(f"[{f.fid}] {f.where} · {f.kind} · {owner}'s confidence {f.stated_confidence:.2f} · belief {f.belief:.2f} · {verdict}")
        lines.append(f"    {f.claim}")
        if f.rationale:
            lines.append(f"    why: {_clip(f.rationale)}")
        ev = describe_evidence(owner, f)
        lines.append("    their evidence: " + ("; ".join(ev) if ev else "their reading only") + "  (shown so you can weigh it; you cannot cite their memory ids)")
    lines += [
        "",
        "Rules for this round:",
        "- For each of your findings: hold, revise, concede, or merge. Change position only for a reason you can name in changed_by: another reviewer's finding id, or one of your own memory ids. Conceding to better evidence is good work; conceding to be agreeable is not.",
        "- If one of your findings makes the same point as another reviewer's, use action merge with duplicate_of set to their id. It then counts as your agreement with their finding instead of a second item in the report. Merge only true duplicates: same location, same point.",
        "- For each other reviewer's finding: agree, disagree, or unsure, with a confidence and a reason from your own reading or memory. If your only reason is that they said it, answer unsure.",
        "- For every response, also write the strongest objection to that finding you can build from the target or your memory, even when you agree. If the objection is good, let it move your stance or your confidence. A panel where everyone agrees and nobody can name an objection has stopped reading.",
        "- Add a new finding only if another reviewer's point revealed something you missed, and name it in prompted_by.",
        "- Do not drift toward the middle. If your evidence stands, hold at your confidence. Disagreement left standing is useful to the reader; a false consensus is not.",
        "- Only your own memory ids may appear in supports, opposes and changed_by, and only when the memory says something about that claim. A memory on a nearby topic is not evidence.",
        "",
        "Fill positions, responses, new_findings and notes. This is the debate round: leave understood as an empty string and findings and prior_findings as empty arrays.",
    ]
    return "\n".join(lines)
