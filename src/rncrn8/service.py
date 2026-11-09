"""Use cases shared by the CLI and, later, the MCP server. No printing here."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .config import EngineConfig, SourceType
from .engine import (
    Decision,
    Evidence,
    Witness,
    attribute_outcome,
    combine,
    decide,
)
from .engine.beta import BetaCounts
from .memory import DecisionRecord, MemoryEntry, MemoryStore, ReliabilityRegistry, Stance, Workspace
from .memory.store import SHARED_PERSONA
from .outcome_source import teaches
from .persona import Persona, load_persona


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _persona_or_shared(ws: Workspace, name: str) -> Persona | None:
    from .memory.store import check_persona_name

    check_persona_name(name)
    if name == SHARED_PERSONA:
        return None
    return load_persona(ws, name)


# --- remember ------------------------------------------------------------------------


def remember(
    ws: Workspace,
    persona: str,
    claim: str,
    source: SourceType,
    origin: str,
    *,
    who: str | None = None,
    observed: date | None = None,
    confidence: float | None = None,
    tags: list[str] | None = None,
    root: str | None = None,
    note: str | None = None,
    expires: date | None = None,
) -> MemoryEntry:
    """Record a memory. The prior comes from, in order: what this origin has earned,
    the persona's trust profile, the config's source class prior. A stated
    confidence re-centres the prior without changing how strongly it is held."""
    config = ws.load_config()
    p = _persona_or_shared(ws, persona)
    source_prior = p.prior_for(source, config) if p else config.prior_for(source)

    registry = ReliabilityRegistry(ws.reliability_path)
    learned = registry.get(origin)
    counts: BetaCounts = learned if learned is not None else source_prior.counts
    if confidence is not None:
        counts = counts.recentered(confidence)

    observed = observed or _today()
    if expires is None and source_prior.review_after_days:
        # staleness counts from when we last checked the fact, which is now; age and decay use the
        # publication date separately. A 2025 article found today is old, not stale.
        expires = _today() + timedelta(days=source_prior.review_after_days)

    entry = MemoryEntry(
        persona=persona,
        claim=claim.strip()[:2000],
        source=source,
        origin=origin.strip()[:300],
        who=(who.strip()[:120] if who else None),
        date=observed,
        root=root,  # None for a human source: MemoryEntry derives who|origin, one person in one conversation is one source
        reliability=counts,
        expires_at=expires,
        tags=[str(t)[:60] for t in (tags or [])][:24],
        note=(note[:4000] if note else None),
    )
    if not entry.claim:
        raise ValueError("a memory needs a claim")
    MemoryStore(ws).append(entry)
    return entry


def retire(ws: Workspace, persona: str, memory_id: str, why: str) -> MemoryEntry:
    """Retire a memory the persona can see. The entry stays in the file with the reason, for the record,
    and is never shown to a persona or cited as evidence again. For a fact a re-read of its source no
    longer supports, `ingest` marks it stale instead; retiring is for what a person knows to be wrong."""
    return retire_memory(ws, persona, memory_id, why)[0]


def retire_memory(ws: Workspace, persona: str, memory_id: str, why: str) -> tuple[MemoryEntry, bool]:
    """`retire`, and whether a decision's line was taken out of world.md: False for a decision whose line
    was edited by hand or is gone, which then stays for the person to remove."""
    why = (why or "").strip()
    if not why:
        raise ValueError("say why; the reason is kept with the entry")
    store = MemoryStore(ws)
    entry = store.get(persona, memory_id)
    if entry is None:
        raise KeyError(f"persona '{persona}' has no memory '{memory_id}'")
    retired = entry.model_copy(update={"retired": why[:500]})
    store.append(retired)
    removed = DECISION_TAG in entry.tags and _remove_decision_line(ws, entry.claim, entry.date)
    return retired, removed


_DATED = re.compile(r"\((\d{4}-\d{2}-\d{2})\) ")


def _states(line: str, claim: str) -> str | None:
    """The date of a Decisions bullet that states exactly `claim`, '' for an undated one, else None. The whole
    claim must be the bullet's text, up to the attribution or reason record_decision writes after it: a
    claim that is only part of a longer decision does not match it."""
    body = line[2:].strip()
    found = _DATED.match(body)
    when = found.group(1) if found else ""
    body = body[found.end():] if found else body
    if body == claim or body.startswith(claim + " [") or body.startswith(claim + " Reason:"):
        return when
    return None


def _remove_decision_line(ws: Workspace, claim: str, observed: date | None = None) -> bool:
    """Take one line stating `claim` out of world.md's Decisions section, so a retired decision stops being
    read as settled; a line with the decision's own date first. One line per retire: a decision recorded
    twice has two lines and two memories."""
    world = ws.read_world()
    lines = world.split("\n")
    target = _one_line(claim)
    in_section = False
    matches: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if line.startswith("## "):
            in_section = line.startswith("## Decisions")
            continue
        if in_section and line.startswith("- "):
            when = _states(line, target)
            if when is not None:
                matches.append((i, when))
    if not matches:
        return False
    wanted = observed.isoformat() if observed else None
    index = next((i for i, when in matches if when == wanted), matches[0][0])
    del lines[index]
    ws.world_path.write_text("\n".join(lines), encoding="utf-8")
    return True


def find_decision(ws: Workspace, claim: str) -> MemoryEntry | None:
    """A recorded decision with this claim, case and spacing aside, if there is one."""
    key = " ".join(claim.lower().split()).rstrip(" .!?;:,")
    for e in MemoryStore(ws).load(SHARED_PERSONA):
        if DECISION_TAG in e.tags and " ".join(e.claim.lower().split()).rstrip(" .!?;:,") == key:
            return e
    return None


# --- decide --------------------------------------------------------------------------


DECISION_TAG = "decision"
_DECISIONS_PLACEHOLDER = "- (date) decision, and why"


def _one_line(text: str) -> str:
    """Free text on one line: runs of whitespace and control characters become one space. A newline
    in a claim or a reason would otherwise write a second line into world.md, and `## ` at the start
    of it a new section that every persona would read as settled."""
    return re.sub(r"[\s\x00-\x1f\x7f]+", " ", text).strip()


def record_decision(
    ws: Workspace,
    claim: str,
    *,
    who: str | None = None,
    origin: str,
    when: date | None = None,
    why: str | None = None,
    source: SourceType = SourceType.HUMAN,
    note: str | None = None,
    tags: list[str] | None = None,
) -> MemoryEntry:
    """Record a settled decision: a shared memory tagged `decision`, and a dated line under `## Decisions`
    in world.md, so every persona treats it as settled rather than as one more claim. A person's decision is
    a human memory naming who and where; one found stated in a project file is a project memory whose origin
    is that file, and its line in world.md names the file."""
    when = when or _today()
    entry = remember(ws, SHARED_PERSONA, claim, source, origin, who=who, observed=when, tags=[DECISION_TAG, *(tags or [])], note=note if note is not None else why)
    reason = _one_line(why or "")
    if reason and reason[-1] not in ".!?":
        reason += "."
    attribution = f"[{_one_line(who)}, {_one_line(origin)}]" if who and who.strip() else f"[{_one_line(origin)}]"
    line = f"- ({when.isoformat()}) {_one_line(entry.claim)} {attribution}" + (f" Reason: {reason}" if reason else "")
    world = ws.read_world()
    if "## Decisions" in world:
        head, heading, tail = world.partition("## Decisions")
        tail_lines = tail.splitlines()
        heading_rest, body = (tail_lines[0] if tail_lines else ""), tail_lines[1:]
        # the section ends at the next heading; bullets under later headings are not decisions
        next_heading = next((i for i, l in enumerate(body) if l.startswith("## ")), len(body))
        section, rest_lines = body[:next_heading], body[next_heading:]
        # people annotate this file by hand: everything in the section stays, except the template placeholder
        kept = [l for l in section if l.strip() != _DECISIONS_PLACEHOLDER]
        while kept and not kept[0].strip():
            kept.pop(0)
        while kept and not kept[-1].strip():
            kept.pop()
        # newest decision first, after any introductory text, so hand-written notes keep their place
        first_bullet = next((i for i, l in enumerate(kept) if l.startswith("- ")), len(kept))
        kept.insert(first_bullet, line)
        rest = "\n".join(rest_lines).strip("\n")
        world = f"{head}{heading}{heading_rest}\n\n" + "\n".join(kept) + "\n" + (f"\n{rest}\n" if rest else "")
    else:
        world = world.rstrip() + f"\n\n## Decisions\n\n{line}\n"
    ws.world_path.write_text(world, encoding="utf-8")
    return entry


# --- judge ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Judgement:
    persona: str
    claim: str
    evidence: Evidence
    decision: Decision
    witnesses: list[tuple[MemoryEntry, Stance]]
    record: DecisionRecord | None


def witness_from_entry(entry: MemoryEntry, stance: Stance, persona: Persona | None, config: EngineConfig, today: date) -> Witness:
    prior = persona.prior_for(entry.source, config) if persona else config.prior_for(entry.source)
    return Witness(
        ref=entry.id,
        reliability=entry.reliability.mean,
        supports=(stance == Stance.SUPPORTS),
        age_days=entry.age_days(today),
        halflife_days=prior.halflife_days,
        cluster=entry.cluster_key,
        stale=entry.is_stale(today),
    )


def judge(
    ws: Workspace,
    persona: str,
    claim: str,
    supports: list[str],
    opposes: list[str],
    *,
    record: bool = True,
    today: date | None = None,
) -> Judgement:
    """Weigh named memories for and against a claim and decide whether to raise it."""
    config = ws.load_config()
    p = _persona_or_shared(ws, persona)
    store = MemoryStore(ws)
    today = today or _today()

    both = set(supports) & set(opposes)
    if both:
        raise ValueError(f"memory id(s) on both sides: {', '.join(sorted(both))}")
    pairs: list[tuple[MemoryEntry, Stance]] = []
    visible = {e.id: e for e in store.load_visible(persona)}  # one read, however many ids
    for ref, stance in [(r, Stance.SUPPORTS) for r in dict.fromkeys(supports)] + [(r, Stance.OPPOSES) for r in dict.fromkeys(opposes)]:
        entry = visible.get(ref)
        if entry is None:
            raise KeyError(f"persona '{persona}' has no memory '{ref}'")
        pairs.append((entry, stance))

    witnesses = [witness_from_entry(e, s, p, config, today) for e, s in pairs]
    evidence = combine(witnesses, config.prior_belief, config.evidence_params())
    loss = p.loss_for(config) if p else config.default_loss
    decision = decide(evidence.belief, loss)

    rec = None
    by_ref = {e.id: e for e, _ in pairs}
    if record:
        rec = DecisionRecord(
            persona=persona,
            claim=claim,
            prior=evidence.prior,
            log_odds=evidence.log_odds,
            belief=evidence.belief,
            threshold=decision.threshold,
            raise_finding=decision.raise_finding,
            severity=decision.severity,
            contributions=[
                {
                    "ref": c.ref,
                    "supports": c.supports,
                    "reliability": c.reliability,
                    "weight": c.weight,
                    "decay": c.decay,
                    "dependence": c.dependence,
                    "stale_factor": c.stale_factor,
                    "contribution": c.contribution,
                    "label": f"{c.ref} {by_ref[c.ref].source.value}",
                    "persona": persona,
                }
                for c in evidence.contributions
            ],
        )
        store.append_decision(rec)
    return Judgement(persona=persona, claim=claim, evidence=evidence, decision=decision, witnesses=pairs, record=rec)


# --- outcome -------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeResult:
    record: DecisionRecord
    updated: list[MemoryEntry]
    """Memories whose trust moved. Empty for an outcome that teaches nothing."""
    origin_updates: dict[str, BetaCounts]
    panel_losses: dict[str, float]
    panel_weights: dict[str, float]
    linked: list[MemoryEntry] | None = None
    """Memories that now link to the outcome. For a teaching outcome these are the `updated` ones."""
    replaced: str | None = None
    """The source of an earlier, unconfirmed outcome that a person's verdict replaced."""


def resolve_decision_id(ws: Workspace, ident: str) -> str:
    """Accept a decision id (d-...) or a review finding id (r-<review>:<persona>:F<n>)."""
    import json as _json

    if ident.startswith("d-"):
        return ident
    if ident.startswith("r-") and ident.count(":") >= 2:
        review_id, persona, fnum = ident.split(":", 2)
        path = ws.reviews_dir / review_id / "review.json"
        if not path.exists():
            raise KeyError(f"no review '{review_id}'")
        data = _json.loads(path.read_text(encoding="utf-8"))
        fid = f"{persona}:{fnum}"
        for p in data.get("personas", []):
            for f in p.get("findings", []):
                if f.get("fid") == fid:
                    if not f.get("decision_id"):
                        raise KeyError(f"finding {fid} in {review_id} has no recorded decision (dry run?)")
                    return f["decision_id"]
        raise KeyError(f"no finding {fid} in review {review_id}")
    raise KeyError(f"'{ident}' is neither a decision id (d-...) nor a review finding id (r-...:persona:F1)")


def record_outcome(
    ws: Workspace,
    decision_id: str,
    claim_true: bool,
    note: str | None = None,
    *,
    learning_rate: float | None = None,
    source: str = "explicit",
) -> OutcomeResult:
    """Close the loop: the witnesses that produced this decision earn or lose trust, and the
    personas whose readings took a side move in the panel weights."""
    from .track import PanelState

    from .memory.lock import locked

    config = ws.load_config()
    store = MemoryStore(ws)
    decision_id = resolve_decision_id(ws, decision_id)
    with locked(ws.root / "state"):
        return _record_outcome_locked(ws, store, config, decision_id, claim_true, note, learning_rate, source)


def _record_outcome_locked(ws, store, config, decision_id, claim_true, note, learning_rate, source) -> "OutcomeResult":
    from .track import PanelState

    rec = store.get_decision(decision_id)
    if rec is None:
        raise KeyError(f"no decision '{decision_id}'")
    replaced: str | None = None
    if rec.outcome is not None:
        # A person's verdict, or a check they saw, replaces an unconfirmed one (a loop, a self-check, a commit
        # closure), which moved no trust and so has nothing to undo; the earlier snapshot stays in the log.
        # Anything else would let an unconfirmed outcome lock out the only kind that teaches.
        if teaches(rec.outcome_source) or not teaches(source):
            raise ValueError(f"decision '{decision_id}' already has an outcome ({rec.outcome_source or 'explicit'})")
        replaced = rec.outcome_source
    if rec.withdrawn:
        raise ValueError(f"decision '{decision_id}' was withdrawn in the debate (conceded or merged into another finding); record the outcome on the finding that stands")
    lr = learning_rate if learning_rate is not None else config.witness_learning_rate
    # Rebuild the evidence object from the stored contributions so attribution is reproducible.
    # A contribution may belong to another persona (a debate response); "persona" says whose.
    import dataclasses

    from .engine.evidence import Contribution

    fields = {f.name for f in dataclasses.fields(Contribution)}
    owner_of = {c["ref"]: c.get("persona", rec.persona) for c in rec.contributions}
    evidence = Evidence(
        prior=rec.prior,
        log_odds=rec.log_odds,
        belief=rec.belief,
        contributions=tuple(Contribution(**{k: v for k, v in c.items() if k in fields}) for c in rec.contributions),
    )
    updates = attribute_outcome(evidence, claim_true, lr)
    # Only an outcome a person stands behind moves trust. Any other kind is recorded on the decision and
    # linked from the memories it cited, so it stays visible, but no Beta count, origin or weight moves:
    # a self-check is a persona grading itself, a commit closure can only confirm, and new memories inherit origin trust.
    teaching = teaches(source)

    registry = ReliabilityRegistry(ws.reliability_path)
    persona_cache: dict[str, Persona | None] = {}
    updated: list[MemoryEntry] = []
    linked: list[MemoryEntry] = []
    origin_updates: dict[str, BetaCounts] = {}
    for u in updates:
        owner = owner_of.get(u.ref, rec.persona)
        entry = store.get(owner, u.ref)
        if entry is None:
            continue
        if owner not in persona_cache:
            try:
                persona_cache[owner] = _persona_or_shared(ws, owner)
            except FileNotFoundError:
                persona_cache[owner] = None
        p = persona_cache[owner]
        # a replaced, unconfirmed outcome left its own link; the person's verdict takes its place
        confirmed_by = [x for x in entry.confirmed_by if x != rec.id]
        contradicted_by = [x for x in entry.contradicted_by if x != rec.id]
        links = {"confirmed_by": [*confirmed_by, rec.id], "contradicted_by": contradicted_by} if u.agreed else {"confirmed_by": confirmed_by, "contradicted_by": [*contradicted_by, rec.id]}
        if teaching:
            new_counts = entry.reliability.confirmed(u.amount) if u.agreed else entry.reliability.contradicted(u.amount)
            new_entry = entry.model_copy(update={"reliability": new_counts, **links})
            updated.append(new_entry)
        else:
            new_entry = entry.model_copy(update=links)
        store.append(new_entry)
        linked.append(new_entry)

        if teaching:
            fallback = (p.prior_for(entry.source, config) if p else config.prior_for(entry.source)).counts
            origin_updates[entry.origin] = registry.nudge(entry.origin, fallback, u.agreed, u.amount)
    if teaching:
        registry.save()

    panel = PanelState.load(ws.panel_path)
    losses: dict[str, float] = {}
    if teaching:
        losses = panel.update_from_decision(rec, claim_true, config.panel_learning_rate)
        if losses:
            panel.save(ws.panel_path)

    closed = rec.model_copy(
        update={
            "outcome": claim_true,
            "outcome_at": datetime.now(timezone.utc),
            "outcome_note": note,
            "outcome_source": source,
            "outcome_learning_rate": lr,
        }
    )
    store.append_decision(closed)
    return OutcomeResult(record=closed, updated=updated, origin_updates=origin_updates, panel_losses=losses,
                         panel_weights=dict(panel.weights), linked=linked, replaced=replaced)
