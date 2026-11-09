"""Review rounds: every persona in its own call, with its own memory, scored by the engine.

Round 1: each persona reviews the packet. Round 2, the debate: each persona reads the
others and answers them; a change of position must name what changed it. Then each
persona rewrites its own summary. Nothing here knows what the target was.
"""

from __future__ import annotations

import dataclasses
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ..config import EngineConfig, SourceType
from ..engine import Witness, combine, decide
from ..memory import DecisionRecord, MemoryEntry, MemoryStore, Stance, Workspace, new_id
from ..models import ModelAdapter, ModelError, ModelRequest, ModelResponse
from ..packet import ReviewPacket
from ..persona import Persona, list_personas, load_persona
from ..service import record_outcome, remember, witness_from_entry
from ..track import PanelState, fit_calibrators, outcome_notes
from .debate import DebateOutput, build_debate_prompt, parse_debate
from .prompts import PANEL_SCHEMA, build_system_prompt, build_target_block, build_user_prompt, select_memories
from .report import render_report
from .summary import SUMMARY_SCHEMA, build_summary_system, build_summary_user, normalise_summary, write_summary

FINDING_KINDS = ("problem", "risk", "question", "strength")
STATUSES = ("agreed", "disputed", "unchallenged", "held", "dropped", "merged")


# --- records --------------------------------------------------------------------------


class RawFinding(BaseModel):
    """What the model returns for one finding, validated leniently."""

    kind: str = "problem"
    claim: str
    where: str = ""
    rationale: str = ""
    fix: str = ""
    confidence: float = 0.5
    supports: list[str] = Field(default_factory=list)
    opposes: list[str] = Field(default_factory=list)
    prompted_by: list[str] = Field(default_factory=list)


class PriorFindingRecord(BaseModel):
    """A persona's check of one of its own earlier findings against the current target."""

    memory_id: str
    status: str
    because: str = ""
    claim: str | None = None
    decision_id: str | None = None
    recorded: bool = False
    note: str | None = None


class ResponseRecord(BaseModel):
    """Another persona's answer to a finding."""

    persona: str
    stance: str
    confidence: float
    calibrated_confidence: float | None = None
    because: str = ""
    objection: str = ""
    supports: list[str] = Field(default_factory=list)
    opposes: list[str] = Field(default_factory=list)
    dropped_refs: list[str] = Field(default_factory=list)
    memory_id: str | None = None


class FindingRecord(BaseModel):
    fid: str
    round: int = 1
    prompted_by: list[str] = Field(default_factory=list)
    kind: str
    claim: str
    original_claim: str | None = None
    where: str
    rationale: str
    fix: str
    stated_confidence: float
    calibrated_confidence: float
    final_confidence: float
    action: str = "hold"
    because: str = ""
    changed_by: list[str] = Field(default_factory=list)
    merged_into: str | None = Field(default=None, description="another reviewer's finding this one was folded into as agreement")
    merged_from: list[str] = Field(default_factory=list, description="other reviewers' findings folded into this one")
    folded_past_readings: dict[str, int] = Field(default_factory=dict, description="per persona, how many of its own past readings were cited and counted once through its current reading")
    supports: list[str]
    opposes: list[str]
    dropped_refs: list[str] = Field(default_factory=list)
    memory_id: str | None = None
    decision_id: str | None = None
    belief_round1: float
    belief: float
    threshold: float
    raise_finding: bool
    severity: float
    conflict: bool
    status: str = "unchallenged"
    responses: list[ResponseRecord] = Field(default_factory=list)
    contributions: list[dict[str, Any]] = Field(default_factory=list)


class PersonaReviewRecord(BaseModel):
    persona: str
    display_name: str
    model: str | None = None
    understood: str = ""
    notes: str = ""
    debate_notes: str = ""
    findings: list[FindingRecord] = Field(default_factory=list)
    prior_findings: list[PriorFindingRecord] = Field(default_factory=list)
    calibration_points: int = 0
    panel_weight: float | None = None
    error: str | None = None
    debate_error: str | None = None
    summary_error: str | None = None
    summary_updated: bool = False
    refused: bool = False
    refusal: str | None = None
    malformed: int = 0
    memories_shown: int = 0
    memories_available: int = 0
    calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    call_log: list[dict[str, Any]] = Field(default_factory=list, description="per call: stage, uncached input, output, cache read, cache write")

    @property
    def raised(self) -> list[FindingRecord]:
        return [f for f in self.findings if f.raise_finding]

    @property
    def held(self) -> list[FindingRecord]:
        return [f for f in self.findings if not f.raise_finding]


class ReviewRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("r"))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    title: str
    kind: str
    target: str | None = None
    adapter: str
    dry_run: bool = False
    debated: bool = False
    summaries_updated: bool = False
    warnings: list[str] = Field(default_factory=list)
    personas: list[PersonaReviewRecord] = Field(default_factory=list)

    def all_findings(self) -> list[tuple[PersonaReviewRecord, FindingRecord]]:
        return [(p, f) for p in self.personas for f in p.findings]

    def failed(self) -> list[PersonaReviewRecord]:
        """Personas whose review call failed, so the review has nothing from them."""
        return [p for p in self.personas if p.error]

    def all_failed(self) -> bool:
        return bool(self.personas) and len(self.failed()) == len(self.personas)

    def raised(self) -> list[tuple[PersonaReviewRecord, FindingRecord]]:
        return sorted([(p, f) for p, f in self.all_findings() if f.raise_finding], key=lambda t: t[1].severity, reverse=True)

    def held(self) -> list[tuple[PersonaReviewRecord, FindingRecord]]:
        return sorted([(p, f) for p, f in self.all_findings() if not f.raise_finding], key=lambda t: t[1].belief, reverse=True)

    def by_status(self, status: str) -> list[tuple[PersonaReviewRecord, FindingRecord]]:
        return sorted([(p, f) for p, f in self.all_findings() if f.status == status], key=lambda t: t[1].severity, reverse=True)


@dataclass
class RunOutput:
    review: ReviewRecord
    review_dir: Path | None
    prompts: dict[str, tuple[str, str]]


# --- helpers ---------------------------------------------------------------------------


def resolve_panel(ws: Workspace, names: list[str] | None, kind: str) -> tuple[list[Persona], list[str]]:
    warnings: list[str] = []
    if names:
        personas = []
        for n in dict.fromkeys(n.strip() for n in names if n and n.strip()):  # a name given twice is one persona
            try:
                p = load_persona(ws, n)
            except FileNotFoundError as exc:
                raise ValueError(str(exc)) from exc
            if not p.reviews(kind):
                warnings.append(f"{p.name} does not normally review '{kind}' targets (applies_to: {', '.join(p.applies_to)}); included because you named it")
            personas.append(p)
        return personas, warnings
    configured = ws.load_config().review.panels.get(kind) or []
    if configured:
        chosen = []
        for n in dict.fromkeys(configured):
            try:
                chosen.append(load_persona(ws, n))
            except (FileNotFoundError, ValueError):
                warnings.append(f"config.yaml review.panels.{kind} names '{n}', which is not a persona here; left out")
        if chosen:
            return chosen, warnings
    personas = [p for p in list_personas(ws) if p.reviews(kind)]
    if not personas:
        raise ValueError(f"no persona applies to '{kind}' targets; create one or name a panel with --panel")
    return personas, warnings


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))




def parse_findings(raw: Any, limit: int, floor: float, ceiling: float) -> tuple[list[RawFinding], int]:
    findings: list[RawFinding] = []
    malformed = 0
    if not isinstance(raw, list):
        return findings, 1 if raw else 0
    for item in raw:
        try:
            f = RawFinding.model_validate(item)
        except ValidationError:
            malformed += 1
            continue
        if not f.claim.strip() or not math.isfinite(f.confidence):
            malformed += 1
            continue
        if f.kind not in FINDING_KINDS:
            f.kind = "problem"
        f.confidence = _clamp(float(f.confidence), floor, ceiling)
        f.claim = f.claim.strip()[:600]
        f.where = " ".join(f.where.split())[:200]
        f.rationale = f.rationale.strip()[:2000]
        f.fix = f.fix.strip()[:2000]
        f.supports = [s for s in dict.fromkeys(f.supports) if isinstance(s, str)][:50]
        f.opposes = [s for s in dict.fromkeys(f.opposes) if isinstance(s, str) and s not in f.supports][:50]
        f.prompted_by = [s for s in f.prompted_by if isinstance(s, str)][:10]
        findings.append(f)
        if len(findings) >= limit:
            break
    return findings, malformed


def _joined(blocks: list[str]) -> str:
    return "\n\n----- cache boundary -----\n\n".join(blocks)


def _add_tokens(rec: PersonaReviewRecord, resp: ModelResponse, stage: str = "") -> None:
    rec.calls += 1
    rec.model = resp.model or rec.model
    for attr in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        v = getattr(resp, attr)
        if v is not None:
            setattr(rec, attr, (getattr(rec, attr) or 0) + v)
    rec.call_log.append({"stage": stage, "input": resp.input_tokens, "output": resp.output_tokens, "cache_read": resp.cache_read_tokens, "cache_write": resp.cache_write_tokens})


@dataclass
class _Ctx:
    persona: Persona
    rec: PersonaReviewRecord
    visible: dict[str, MemoryEntry]
    system: str = ""
    active: bool = False
    debate: DebateOutput | None = None
    given: list[tuple[str, FindingRecord, str, float, str]] = field(default_factory=list)


class _Run:
    def __init__(
        self,
        ws: Workspace,
        packet: ReviewPacket,
        adapter: ModelAdapter,
        personas: list[Persona],
        *,
        dry_run: bool,
        debate: bool,
        summaries: bool,
        max_findings: int | None,
        model: str | None,
        target: str | None,
        today: date,
        warnings: list[str],
    ):
        self.ws = ws
        self.packet = packet
        self.adapter = adapter
        self.config: EngineConfig = ws.load_config()
        self.store = MemoryStore(ws)
        self.persist = not dry_run
        self.debate_enabled = debate
        self.summaries_enabled = summaries and not dry_run
        self.limit = max_findings or self.config.review.max_findings_per_persona
        self.model = model
        self.today = today
        self.world = ws.read_world()
        self.review = ReviewRecord(title=packet.title, kind=packet.kind, target=target, adapter=adapter.name, dry_run=dry_run, warnings=warnings)
        self.target_block = build_target_block(packet)
        self.prompts: dict[str, tuple[str, str]] = {}
        self.ctxs: list[_Ctx] = [
            _Ctx(persona=p, rec=PersonaReviewRecord(persona=p.name, display_name=p.display_name), visible={}) for p in personas
        ]
        self.by_name = {c.persona.name: c for c in self.ctxs}
        self.findings: dict[str, FindingRecord] = {}
        self.owner_of: dict[str, str] = {}
        closed = self.store.load_decisions()
        self.calibrators = fit_calibrators(closed, self.config.calibration) if self.config.calibration.enabled else {}
        self.outcome_notes = outcome_notes(closed)
        self.panel = PanelState.load(ws.panel_path)
        for c in self.ctxs:
            cal = self.calibrators.get(c.persona.name)
            c.rec.calibration_points = cal.n if cal else 0
            c.rec.panel_weight = self.panel.weights.get(c.persona.name)

    def calibrate(self, persona: str, stated: float) -> float:
        cal = self.calibrators.get(persona)
        return cal(stated) if cal else stated

    def panel_scale(self, persona: str) -> float:
        if not self.config.panel.apply_weights:
            return 1.0
        return self.panel.scale_for(persona, self.config.panel.influence)

    # --- plumbing --------------------------------------------------------------------

    def _pool(self):
        return ThreadPoolExecutor(max_workers=max(1, min(self.config.review.parallel, len(self.ctxs))))

    def _request(self, ctx: _Ctx, system: str, user: str, schema: dict[str, Any], blocks: list[str] | None = None) -> ModelRequest:
        return ModelRequest(
            system=system,
            user=user,
            schema=schema,
            max_tokens=self.config.models.max_tokens,
            model=self.model or ctx.persona.model,
            effort=self.config.models.effort,
            label=ctx.persona.name,
            system_blocks=blocks,
        )

    def _review_blocks(self, ctx: _Ctx) -> list[str]:
        """Target first (shared by every persona and both rounds), the persona's own block second."""
        return [self.target_block, ctx.system]

    def _call(self, ctx: _Ctx, request: ModelRequest, stage: str = "") -> tuple[ModelResponse | None, str | None]:
        """Returns (response with data, None) or (None, error text)."""
        try:
            resp = self.adapter.complete(request)
        except ModelError as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001 - a broken adapter must not take the panel down
            return None, f"adapter failure: {type(exc).__name__}: {exc}"
        _add_tokens(ctx.rec, resp, stage)
        if resp.refused:
            return None, "refused: " + (" ".join(x for x in (resp.refusal_category, resp.refusal_explanation) if x) or "no reason given")
        if not isinstance(resp.data, dict):
            return None, "model returned no JSON object"
        return resp, None

    def _summary_text(self, ctx: _Ctx) -> str:
        path = self.ws.persona_dir(ctx.persona.name) / "summary.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    # --- scoring ----------------------------------------------------------------------

    def _memory_witnesses(
        self, ctx: _Ctx, refs: list[str], stance: Stance, seen: set[str], folded: dict[str, int]
    ) -> tuple[list[Witness], list[str], list[str], dict[str, dict[str, Any]]]:
        """Cited memories as witnesses. A persona's own past inferences are the same reader saying
        the same thing again: they are not witnesses, they are counted in `folded` and the persona's
        current reading carries its opinion once."""
        witnesses, used, dropped = [], [], []
        meta: dict[str, dict[str, Any]] = {}
        own_origin = f"inference:{ctx.persona.name}"
        for ref in refs:
            e = ctx.visible.get(ref)
            if e is None:
                dropped.append(ref)
                continue
            if ref in seen:
                continue
            seen.add(ref)
            if e.source == SourceType.INFERENCE and e.origin == own_origin:
                folded[ctx.persona.name] = folded.get(ctx.persona.name, 0) + 1
                used.append(ref)
                continue
            w = witness_from_entry(e, stance, ctx.persona, self.config, self.today)
            whose = "" if e.persona in (ctx.persona.name, "shared") else f" ({e.persona}'s)"
            meta[ref] = {"label": f"{ref} {e.source.value}{whose}", "persona": ctx.persona.name, "role": "memory", "source": e.source.value}
            witnesses.append(w)
            used.append(ref)
        return witnesses, used, dropped, meta

    def _evaluate(self, owner: Persona, witnesses: list[Witness], meta: dict[str, dict[str, Any]]):
        """Contributions keep every raw witness field so a replay can re-score them under another config."""
        evidence = combine(witnesses, self.config.prior_belief, self.config.evidence_params())
        decision = decide(evidence.belief, owner.loss_for(self.config))
        by_ref = {w.ref: w for w in witnesses}
        contributions = []
        for c in evidence.ranked():
            d = dataclasses.asdict(c)
            w = by_ref[c.ref]
            d.update({"age_days": w.age_days, "halflife_days": w.halflife_days, "cluster": w.cluster, "stale": w.stale})
            d.setdefault("label", c.ref)
            d.setdefault("persona", owner.name)
            d.update(meta.get(c.ref, {}))
            contributions.append(d)
        return evidence, decision, contributions

    def _reading_witness(self, ctx: _Ctx, ref: str, stated: float, supports: bool, fid: str) -> tuple[Witness, dict[str, Any]]:
        """The persona's own judgement as a witness: stated confidence run through its calibrator,
        scaled by its panel weight. Both numbers are kept."""
        prior = ctx.persona.prior_for(SourceType.INFERENCE, self.config)
        calibrated = self.calibrate(ctx.persona.name, stated)
        w = Witness(
            ref=ref,
            reliability=calibrated,
            supports=supports,
            age_days=0.0,
            halflife_days=prior.halflife_days,
            cluster=f"reading:{fid}",
            scale=self.panel_scale(ctx.persona.name),
            allow_negative=True,  # a reading is a probability: a persona wrong two times in three at 0.6 is, at 0.6, evidence of the opposite
        )
        meta = {"persona": ctx.persona.name, "role": "reading", "source": "reading", "stated": stated}
        return w, meta

    def _score_new(self, ctx: _Ctx, f: RawFinding, fid: str, round_no: int) -> FindingRecord:
        seen: set[str] = set()
        folded: dict[str, int] = {}
        sup_w, sup_ids, dropped_a, meta = self._memory_witnesses(ctx, f.supports, Stance.SUPPORTS, seen, folded)
        opp_w, opp_ids, dropped_b, meta_b = self._memory_witnesses(ctx, f.opposes, Stance.OPPOSES, seen, folded)
        meta.update(meta_b)

        calibrated = self.calibrate(ctx.persona.name, f.confidence)
        memory_id: str | None = None
        self_ref = f"{ctx.persona.name}:reading:{fid}"
        if self.persist:
            entry = remember(
                self.ws,
                ctx.persona.name,
                f.claim,
                SourceType.INFERENCE,
                origin=f"inference:{ctx.persona.name}",
                confidence=calibrated,
                tags=[self.review.id, "finding", f.kind, f"kind:{self.packet.kind}", self.packet.target_tag] + (["debate"] if round_no > 1 else []),
                root=self.review.id,
                note=(f"{f.where}: {f.rationale}".strip(": "))[:500] or None,
                observed=self.today,
            )
            memory_id = self_ref = entry.id
        own, own_meta = self._reading_witness(ctx, self_ref, f.confidence, True, fid)
        meta[self_ref] = {"label": "own reading", **own_meta}
        witnesses = sup_w + opp_w + [own]
        evidence, decision, contributions = self._evaluate(ctx.persona, witnesses, meta)

        decision_id: str | None = None
        if self.persist:
            rec = DecisionRecord(
                persona=ctx.persona.name,
                claim=f.claim,
                prior=evidence.prior,
                log_odds=evidence.log_odds,
                belief=evidence.belief,
                threshold=decision.threshold,
                raise_finding=decision.raise_finding,
                severity=decision.severity,
                contributions=contributions,
                review_id=self.review.id,
                where=f.where,
                kind=f.kind,
            )
            self.store.append_decision(rec)
            decision_id = rec.id

        fr = FindingRecord(
            fid=fid,
            round=round_no,
            prompted_by=f.prompted_by,
            kind=f.kind,
            claim=f.claim,
            where=f.where,
            rationale=f.rationale,
            fix=f.fix,
            stated_confidence=f.confidence,
            calibrated_confidence=calibrated,
            final_confidence=f.confidence,
            supports=sup_ids,
            opposes=opp_ids,
            dropped_refs=dropped_a + dropped_b,
            memory_id=memory_id,
            decision_id=decision_id,
            belief_round1=evidence.belief,
            belief=evidence.belief,
            threshold=decision.threshold,
            raise_finding=decision.raise_finding,
            severity=decision.severity,
            conflict=evidence.conflict,
            contributions=contributions,
            folded_past_readings=folded,
        )
        self.findings[fid] = fr
        self.owner_of[fid] = ctx.persona.name
        return fr

    def _rescore(self, f: FindingRecord) -> None:
        """Re-run the engine with the owner's memory witnesses, the owner's reading unless conceded,
        and every other persona's reading plus cited memories. Decision uses the owner's loss matrix."""
        owner = self.by_name[self.owner_of[f.fid]]
        seen: set[str] = set()
        folded: dict[str, int] = {}
        sup_w, _, _, meta = self._memory_witnesses(owner, f.supports, Stance.SUPPORTS, seen, folded)
        opp_w, _, _, meta_b = self._memory_witnesses(owner, f.opposes, Stance.OPPOSES, seen, folded)
        meta.update(meta_b)
        witnesses = sup_w + opp_w
        if f.action not in ("concede", "merge"):
            self_ref = f.memory_id or f"{owner.persona.name}:reading:{f.fid}"
            own, own_meta = self._reading_witness(owner, self_ref, f.final_confidence, True, f.fid)
            f.calibrated_confidence = own.reliability
            meta[self_ref] = {"label": "own reading", **own_meta}
            witnesses.append(own)
        for r in f.responses:
            if r.stance not in ("agree", "disagree"):
                continue
            responder = self.by_name[r.persona]
            ref = r.memory_id or f"{r.persona}:reading:{f.fid}"
            w, w_meta = self._reading_witness(responder, ref, r.confidence, r.stance == "agree", f.fid)
            r.calibrated_confidence = w.reliability
            meta[ref] = {"label": f"{r.persona} {r.stance}s", **w_meta}
            witnesses.append(w)
            rs, _, _, m1 = self._memory_witnesses(responder, r.supports, Stance.SUPPORTS, seen, folded)
            ro, _, _, m2 = self._memory_witnesses(responder, r.opposes, Stance.OPPOSES, seen, folded)
            for m in (m1, m2):
                for ref2, info in m.items():
                    if "'s)" not in info["label"]:
                        info["label"] = f"{info['label']} ({r.persona}'s)"
            meta.update(m1)
            meta.update(m2)
            witnesses += rs + ro
        evidence, decision, contributions = self._evaluate(owner.persona, witnesses, meta)
        f.folded_past_readings = folded
        f.belief = evidence.belief
        f.threshold = decision.threshold
        f.raise_finding = decision.raise_finding and f.action not in ("concede", "merge")
        f.severity = decision.severity
        f.conflict = evidence.conflict
        f.contributions = contributions
        if self.persist and f.decision_id:
            original = self.store.get_decision(f.decision_id)
            if original is not None:
                self.store.append_decision(
                    original.model_copy(
                        update={
                            "claim": f.claim,
                            "log_odds": evidence.log_odds,
                            "belief": evidence.belief,
                            "threshold": decision.threshold,
                            "raise_finding": f.raise_finding,
                            "severity": decision.severity,
                            "contributions": contributions,
                            "revision": original.revision + 1,
                            "withdrawn": f.action in ("concede", "merge"),
                            "withdrawn_how": (f"merged into {f.merged_into}" if f.action == "merge" and f.merged_into else ("conceded" if f.action == "concede" else None)),
                            "withdrawn_into": (self.findings[f.merged_into].decision_id if f.action == "merge" and f.merged_into in self.findings else None),
                        }
                    )
                )

    # --- rounds -----------------------------------------------------------------------

    def round1(self) -> None:
        def call(ctx: _Ctx):
            visible = self.store.load_visible(ctx.persona.name)
            ctx.visible = {e.id: e for e in visible}
            shown = select_memories(visible, self.packet, self.config.review.memory_limit, self.config.review.memory_char_budget, self.today, persona=ctx.persona.name)
            ctx.rec.memories_shown = len(shown)
            ctx.rec.memories_available = len(visible)
            ctx.system = build_system_prompt(ctx.persona, self.world, self._summary_text(ctx), shown, self.limit, self.today, all_entries=visible, outcomes=self.outcome_notes)
            user = build_user_prompt(self.packet)
            self.prompts[ctx.persona.name] = (_joined(self._review_blocks(ctx)), user)
            resp, err = self._call(ctx, self._request(ctx, ctx.system, user, PANEL_SCHEMA, blocks=self._review_blocks(ctx)), stage="review")
            if err is not None:
                if err.startswith("refused: "):
                    ctx.rec.refused, ctx.rec.refusal = True, err[len("refused: ") :]
                else:
                    ctx.rec.error = err
            return ctx, resp

        # Warm first: the first persona's call writes the shared target block to the cache; the rest then read it.
        warm = self.config.review.warm_cache and getattr(self.adapter, "supports_cache", False) and len(self.ctxs) > 1
        if warm:
            results = [call(self.ctxs[0])]
            with self._pool() as pool:
                results += list(pool.map(call, self.ctxs[1:]))
        else:
            with self._pool() as pool:
                results = list(pool.map(call, self.ctxs))

        for ctx, resp in results:
            if resp is None:
                continue
            ctx.active = True
            data = resp.data
            ctx.rec.understood = str(data.get("understood", ""))[:2000]
            ctx.rec.notes = str(data.get("notes", ""))[:4000]
            raw, ctx.rec.malformed = parse_findings(data.get("findings"), self.limit, self.config.review.confidence_floor, self.config.review.confidence_ceiling)
            for i, f in enumerate(raw, start=1):
                ctx.rec.findings.append(self._score_new(ctx, f, f"{ctx.persona.name}:F{i}", 1))
            self._apply_prior_findings(ctx, data.get("prior_findings"))

    def _apply_prior_findings(self, ctx: _Ctx, raw: Any) -> None:
        """A persona's verdict on its own earlier findings becomes an implicit outcome on the
        decision that produced them: addressed -> confirmed, wrong -> refuted, unchanged -> nothing."""
        if not isinstance(raw, list):
            return
        own_origin = f"inference:{ctx.persona.name}"
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("memory_id"), str):
                ctx.rec.malformed += 1
                continue
            mid = item["memory_id"]
            status = str(item.get("status", "unchanged"))
            because = str(item.get("because", ""))[:500]
            if mid in seen:
                continue
            seen.add(mid)
            pf = PriorFindingRecord(memory_id=mid, status=status, because=because)
            entry = ctx.visible.get(mid)
            if entry is None or entry.origin != own_origin or "finding" not in entry.tags:
                pf.note = "not one of this persona's own findings; ignored"
                ctx.rec.prior_findings.append(pf)
                continue
            if self.packet.target_tag not in entry.tags:
                # the prompt asks only about findings on this same target; a finding on another document
                # cannot be addressed by this one, so a verdict on it records nothing
                pf.claim = entry.claim[:300]
                pf.note = "a finding on a different target; ignored"
                ctx.rec.prior_findings.append(pf)
                continue
            pf.claim = entry.claim[:300]
            if status not in ("addressed", "wrong"):
                pf.status = "unchanged" if status not in ("addressed", "unchanged", "wrong") else status
                ctx.rec.prior_findings.append(pf)
                continue
            citing = [d for d in self.store.decisions_citing(mid) if d.persona == ctx.persona.name]
            open_decisions = [d for d in citing if d.outcome is None]
            if not open_decisions:
                closed = [d for d in citing if d.outcome is not None]
                pf.note = (
                    f"outcome already recorded: {'confirmed' if closed[0].outcome else 'refuted'} ({closed[0].outcome_source or 'explicit'})"
                    if closed
                    else "no open decision behind this memory"
                )
                ctx.rec.prior_findings.append(pf)
                continue
            pf.decision_id = open_decisions[0].id
            if self.persist:
                try:
                    record_outcome(
                        self.ws,
                        pf.decision_id,
                        status == "addressed",
                        note=f"{ctx.persona.name} saw it {status} in review {self.review.id}: {because}".strip(": "),
                        learning_rate=self.config.outcomes.implicit_learning_rate,
                        source=f"implicit:{self.review.id}",
                    )
                    pf.recorded = True
                except (KeyError, ValueError) as exc:
                    pf.note = str(exc)
            ctx.rec.prior_findings.append(pf)

    def debate(self) -> None:
        active = [c for c in self.ctxs if c.active]
        if len(active) < 2:
            return
        self.review.debated = True

        def describe(owner_name: str, f: FindingRecord) -> list[str]:
            owner = self.by_name[owner_name]
            out = []
            for ref in f.supports + f.opposes:
                e = owner.visible.get(ref)
                if e is None:
                    continue
                side = "for" if ref in f.supports else "against"
                who = f", {e.who}" if e.who else ""
                out.append(f'({side}: {e.source.value} {e.confidence:.2f}, {e.origin}{who}) "{e.claim[:140]}"')
            return out

        def describe_mine(owner_name: str, f: FindingRecord) -> list[str]:
            owner = self.by_name[owner_name]
            out = []
            for ref in f.supports + f.opposes:
                e = owner.visible.get(ref)
                if e is None:
                    continue
                side = "for" if ref in f.supports else "against"
                out.append(f'{ref} ({side}: {e.source.value} {e.confidence:.2f}) "{e.claim[:100]}"')
            return out

        def call(ctx: _Ctx):
            mine = list(ctx.rec.findings)
            others = [(c.persona.name, f) for c in active if c is not ctx for f in c.rec.findings]
            user = build_debate_prompt(self.packet, ctx.persona.name, mine, others, lambda o, f: describe_mine(o, f) if o == ctx.persona.name else describe(o, f))
            self.prompts[f"{ctx.persona.name}:debate"] = (_joined(self._review_blocks(ctx)), user)
            resp, err = self._call(ctx, self._request(ctx, ctx.system, user, PANEL_SCHEMA, blocks=self._review_blocks(ctx)), stage="debate")
            if err is not None:
                ctx.rec.debate_error = err
                return ctx
            ctx.debate = parse_debate(resp.data)
            ctx.rec.debate_notes = ctx.debate.notes
            ctx.rec.malformed += ctx.debate.malformed
            return ctx

        with self._pool() as pool:
            list(pool.map(call, active))

        # positions on one's own findings: collect first, then apply hold/revise/concede, then
        # resolve merges all at once so the result does not depend on which persona was read first
        positions: dict[str, RawPosition] = {}
        for ctx in active:
            if ctx.debate is None:
                continue
            for pos in ctx.debate.positions:
                f = self.findings.get(pos.finding_id)
                if f is None or self.owner_of[f.fid] != ctx.persona.name:
                    continue
                positions[f.fid] = pos

        for fid, pos in positions.items():
            if pos.action == "merge":
                continue
            f = self.findings[fid]
            owner = self.by_name[self.owner_of[fid]]
            f.action = pos.action
            f.because = pos.because[:1000]
            f.changed_by = [c for c in pos.changed_by if isinstance(c, str)]
            if pos.confidence is not None and math.isfinite(pos.confidence):
                f.final_confidence = _clamp(float(pos.confidence), self.config.review.confidence_floor, self.config.review.confidence_ceiling)
            if pos.action == "revise" and pos.revised_claim.strip():
                f.original_claim = f.original_claim or f.claim
                f.claim = pos.revised_claim.strip()[:600]
            if self.persist and f.memory_id and pos.action in ("revise", "concede"):
                entry = self.store.get(owner.persona.name, f.memory_id)
                if entry is not None:
                    update: dict[str, Any] = {"reliability": entry.reliability.recentered(f.final_confidence)}
                    if pos.action == "revise":
                        update["claim"] = f.claim
                        update["note"] = ((entry.note or "") + f" [revised in debate: {pos.because[:200]}]").strip()
                    else:
                        update["tags"] = [*entry.tags, "conceded"]
                        update["note"] = ((entry.note or "") + f" [conceded in debate: {pos.because[:200]}]").strip()
                    self.store.append(entry.model_copy(update=update))

        self._resolve_merges({fid: pos for fid, pos in positions.items() if pos.action == "merge"})

        # responses to others' findings
        for ctx in active:
            if ctx.debate is None:
                continue
            answered: set[str] = {f.merged_into for f in self.findings.values() if self.owner_of[f.fid] == ctx.persona.name and f.merged_into}
            for r in ctx.debate.responses:
                f = self.findings.get(r.finding_id)
                if f is None or self.owner_of[f.fid] == ctx.persona.name or r.finding_id in answered:
                    continue
                if not math.isfinite(r.confidence):
                    ctx.rec.malformed += 1
                    continue
                answered.add(r.finding_id)
                conf = _clamp(float(r.confidence), self.config.review.confidence_floor, self.config.review.confidence_ceiling)
                dropped = [x for x in r.supports + r.opposes if x not in ctx.visible]
                supports = [x for x in r.supports if x in ctx.visible]
                opposes = [x for x in r.opposes if x in ctx.visible]
                memory_id = None
                if self.persist and r.stance in ("agree", "disagree"):
                    entry = remember(
                        self.ws,
                        ctx.persona.name,
                        f"{r.stance}s with {self.owner_of[f.fid]} [{f.fid}]: {f.claim}",
                        SourceType.INFERENCE,
                        origin=f"inference:{ctx.persona.name}",
                        confidence=conf,
                        tags=[self.review.id, "debate", r.stance, f"kind:{self.packet.kind}", self.packet.target_tag],
                        root=self.review.id,
                        note=r.because[:500] or None,
                        observed=self.today,
                    )
                    memory_id = entry.id
                objection = " ".join(r.objection.split())[:600]
                if objection.lower().strip(" .") in ("", "none", "n/a", "no objection"):
                    objection = ""
                f.responses.append(
                    ResponseRecord(persona=ctx.persona.name, stance=r.stance, confidence=conf, because=r.because[:1000], objection=objection, supports=supports, opposes=opposes, dropped_refs=dropped, memory_id=memory_id)
                )
                ctx.given.append((self.owner_of[f.fid], f, r.stance, conf, r.because[:300]))

        for f in list(self.findings.values()):
            self._rescore(f)

        # new findings prompted by the debate
        for ctx in active:
            if ctx.debate is None or not ctx.debate.new_findings:
                continue
            room = max(0, self.limit - len(ctx.rec.findings))
            raw, malformed = parse_findings(ctx.debate.new_findings, room, self.config.review.confidence_floor, self.config.review.confidence_ceiling)
            ctx.rec.malformed += malformed
            start = len(ctx.rec.findings)
            for i, f in enumerate(raw, start=start + 1):
                ctx.rec.findings.append(self._score_new(ctx, f, f"{ctx.persona.name}:F{i}", 2))

    def _final_target(self, fid: str, wants: dict[str, str]) -> str | None:
        """Follow merge requests to the finding that survives. A cycle resolves to its smallest fid.
        None when the chain leads nowhere valid."""
        seen: list[str] = [fid]
        cur = wants.get(fid)
        while cur is not None:
            if cur in seen:
                cycle = seen[seen.index(cur) :]
                primary = min(cycle)
                return None if primary == fid else primary
            if cur not in self.findings:
                return None
            seen.append(cur)
            cur = wants.get(cur)
        return seen[-1] if len(seen) > 1 else None

    def _resolve_merges(self, merges: dict[str, RawPosition]) -> None:
        wants = {fid: pos.duplicate_of for fid, pos in merges.items()}
        for fid in sorted(merges):
            pos = merges[fid]
            f = self.findings[fid]
            owner = self.owner_of[fid]
            ctx = self.by_name[owner]
            final = self._final_target(fid, wants)
            target = self.findings.get(final) if final else None
            if target is None or self.owner_of[target.fid] == owner or target.action == "concede":
                f.action = "hold"  # nothing valid to merge into, or the target was given up: keep the finding
                f.because = pos.because[:1000]
                continue
            f.action = "merge"
            f.merged_into = target.fid
            f.because = pos.because[:1000]
            if pos.confidence is not None and math.isfinite(pos.confidence):
                f.final_confidence = _clamp(float(pos.confidence), self.config.review.confidence_floor, self.config.review.confidence_ceiling)
            target.merged_from.append(f.fid)
            if not any(r.persona == owner for r in target.responses):  # one reader counts once per finding
                target.responses.append(
                    ResponseRecord(
                        persona=owner,
                        stance="agree",
                        confidence=f.final_confidence,
                        because=(pos.because or f"raised the same point as [{f.fid}]")[:1000],
                        supports=list(f.supports),
                        opposes=list(f.opposes),
                        memory_id=f.memory_id,
                    )
                )
            if self.persist and f.memory_id:
                entry = self.store.get(owner, f.memory_id)
                if entry is not None:
                    self.store.append(entry.model_copy(update={"tags": [*entry.tags, "merged"], "note": ((entry.note or "") + f" [merged into {target.fid}]").strip()}))

    def classify(self) -> None:
        for f in self.findings.values():
            if f.action == "merge":
                f.status = "merged"
            elif f.action == "concede":
                f.status = "dropped"
            elif not f.raise_finding:
                f.status = "held"
            elif any(r.stance == "disagree" for r in f.responses):
                f.status = "disputed"
            elif any(r.stance == "agree" for r in f.responses):
                f.status = "agreed"
            else:
                f.status = "unchallenged"
        for ctx in self.ctxs:
            ctx.rec.findings.sort(key=lambda fr: fr.severity, reverse=True)

    def summaries(self) -> None:
        active = [c for c in self.ctxs if c.active]
        if not active:
            return

        def call(ctx: _Ctx):
            system = build_summary_system(ctx.persona, self.config.review.summary_max_words)
            user = build_summary_user(ctx.persona, self._summary_text(ctx), self.review, ctx.rec, ctx.given)
            self.prompts[f"{ctx.persona.name}:summary"] = (system, user)
            resp, err = self._call(ctx, self._request(ctx, system, user, SUMMARY_SCHEMA), stage="summary")
            if err is not None:
                ctx.rec.summary_error = err
                return
            text = resp.data.get("summary_markdown")
            if not isinstance(text, str) or not text.strip():
                ctx.rec.summary_error = "model returned an empty summary"
                return
            limit = self.config.review.summary_max_chars
            if len(text) > limit:
                text = text[:limit].rstrip() + "\n\n(summary cut at the size limit)\n"
            write_summary(self.ws, ctx.persona.name, normalise_summary(text, ctx.persona), self.review.id)
            ctx.rec.summary_updated = True

        with self._pool() as pool:
            list(pool.map(call, active))
        self.review.summaries_updated = any(c.rec.summary_updated for c in active)

    def finish(self) -> RunOutput:
        self.review.personas = [c.rec for c in self.ctxs]
        review_dir: Path | None = None
        if self.persist:
            review_dir = self.ws.reviews_dir / self.review.id
            (review_dir / "prompts").mkdir(parents=True, exist_ok=True)
            (review_dir / "packet.json").write_text(self.packet.model_dump_json(indent=2), encoding="utf-8")
            for key, (system, user) in self.prompts.items():
                name, _, stage = key.partition(":")
                if stage == "":
                    (review_dir / "prompts" / f"{name}.system.txt").write_text(system, encoding="utf-8")
                    (review_dir / "prompts" / f"{name}.user.txt").write_text(user, encoding="utf-8")
                elif stage == "debate":
                    (review_dir / "prompts" / f"{name}.debate.user.txt").write_text(user, encoding="utf-8")
                else:
                    (review_dir / "prompts" / f"{name}.{stage}.system.txt").write_text(system, encoding="utf-8")
                    (review_dir / "prompts" / f"{name}.{stage}.user.txt").write_text(user, encoding="utf-8")
            (review_dir / "review.json").write_text(self.review.model_dump_json(indent=2), encoding="utf-8")
            (review_dir / "report.md").write_text(render_report(self.review), encoding="utf-8")
        return RunOutput(review=self.review, review_dir=review_dir, prompts=self.prompts)


def run_review(
    ws: Workspace,
    packet: ReviewPacket,
    adapter: ModelAdapter,
    *,
    panel: list[str] | None = None,
    dry_run: bool = False,
    debate: bool | None = None,
    summaries: bool | None = None,
    max_findings: int | None = None,
    model: str | None = None,
    target: str | Path | None = None,
    today: date | None = None,
) -> RunOutput:
    config = ws.load_config()
    personas, warnings = resolve_panel(ws, panel, packet.kind)
    run = _Run(
        ws,
        packet,
        adapter,
        personas,
        dry_run=dry_run,
        debate=config.review.debate if debate is None else debate,
        summaries=config.review.update_summaries if summaries is None else summaries,
        max_findings=max_findings,
        model=model,
        target=str(target) if target else None,
        today=today or datetime.now(timezone.utc).date(),
        warnings=warnings,
    )
    run.round1()
    if run.debate_enabled:
        run.debate()
    run.classify()
    if run.summaries_enabled:
        run.summaries()
    return run.finish()


def load_review(ws: Workspace, review_id: str) -> ReviewRecord:
    path = ws.reviews_dir / review_id / "review.json"
    if not path.exists():
        raise FileNotFoundError(f"no review '{review_id}'")
    return ReviewRecord.model_validate_json(path.read_text(encoding="utf-8"))


def list_reviews(ws: Workspace) -> list[ReviewRecord]:
    if not ws.reviews_dir.exists():
        return []
    out = []
    for d in sorted(ws.reviews_dir.iterdir()):
        if (d / "review.json").exists():
            out.append(ReviewRecord.model_validate_json((d / "review.json").read_text(encoding="utf-8")))
    return out
