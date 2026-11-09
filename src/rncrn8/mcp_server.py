"""MCP server: the panel, run from inside Claude Code, Cursor or any MCP client.

    rncrn8 mcp [--workspace /path/to/project]

The assistant is the go-between: the user talks to it, it runs the panel through these tools, puts
what the panel says in plain words, and records what the user answers. Every tool resolves the
workspace from --workspace, else the project folder the client names (CLAUDE_PROJECT_DIR), else the
current directory, so one registration serves every project that has a workspace.

The server never writes to stdout itself: stdout is the protocol.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .adapters import AdapterError, build_packet, git_diff_packet, text_packet
from .config import SourceType
from .memory import MemoryStore, Workspace
from .memory.legacy import git
from .memory.lock import LockTimeout
from .memory.store import SHARED_PERSONA
from .models import ModelError, make_adapter
from .outcome_source import FROM_CALLER, teaches
from .persona import list_personas
from .review import run_review
from .review.digest import digest
from .review.report import render_report
from .review.runner import list_reviews
from .service import record_decision as decide_service
from .service import record_outcome
from .service import remember as remember_service

INSTRUCTIONS = """\
rncrn8 is a review panel: several personas, each with its own memory, review the user's work and learn from what turns out true. You are the go-between. The user talks to you; you run rncrn8 through these tools, put what the panel says in plain words, and record what the user tells you. Once rncrn8 is installed and connected, the user should not need its command line.

Setup. When the project has no panel yet, or the user asks to set rncrn8 up, call `setup` and show its proposal in plain words: what the panel will be told and which file says it, what was left out and why, and the suggested panel with why each persona fits. Ask whether to go ahead, whether to drop anyone, and whether to add anyone (a name and a role is enough), then call `setup_apply` with the answers. `add_persona` adds one later.

Reviews. When the user asks for a review of a branch, uncommitted changes, a file, a plan or a pitch, call `review`; with no target it reviews the current branch against its base. A review makes model calls and takes a minute or more; say so before you start. Then report in plain words, in this order:
1. Problems the panel agrees on: one line each, saying what is wrong, where, and the fix.
2. Disputed findings: the claim, who disagrees and why, and how to settle it, often with a test you can write.
3. Questions: when the code can answer one, such as whether a path is ever reached, check it yourself and say what you found; ask the user only what the code cannot settle.
4. In one line: what was raised that nobody challenged, and how many were held back as too uncertain.
Leave out beliefs, thresholds, ids and persona names unless they help or the user asks. End with the next step on offer: fix, test, or answer.

Recording. Keep the decision ids from the review to yourself and use them to record:
- `answer` when the user says findings were right or wrong, or answers a question: one entry for every finding they answered, all in one call, each with their reason in their own words. When they state something the panel should know from now on, also pass it as `fact`, one self-contained sentence.
- Record only what the user said (source 'user'), or the result of a check you ran and showed them, such as a test or a command's output (source 'check'). Never record your own opinion as an outcome. Only an agent running unattended in an automated loop, with no person present, records with source 'loop', which is kept but teaches nothing. Then say in one line what you recorded, and name the findings `answer` lists as still open.
- After changes to reviewed code are committed, call `followup` with the review id; it closes findings whose cited lines changed. Tell the user which ones.
- When the user states a rule or a settled decision, such as "notes never go on invoices" or "we always store times in UTC", record it with `decide` and say so.

`status` shows what is still open and each persona's track record, without model calls. Use it when the user asks how the panel is doing or what is left.

Beyond code. Plans, pitches and strategies are reviewed the same way: pass the text, or the path of a document. If the panel has no one for them, offer ready-made personas with `add_seed_personas` (the pitch panel: critic, investor, customer, counsel, auditor; `seed_personas` lists all), or write one with `add_persona`.

Memory from outside. When the user shares what someone told them, a call, an interview or a meeting, offer `ingest_notes` with who said it and where. When a review needs facts from outside, offer `research`: one persona searches the web on a budget and remembers what it found with its sources. It costs more than a review call, so ask first.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True)
KEY_MISSING = (
    "The panel cannot call the model: no Anthropic key where the rncrn8 server runs. Tell the user to set one: "
    + 'setx RNCRN8_API_KEY "sk-ant-api03-..." in PowerShell, then restart VS Code or the terminal Claude Code runs in; export RNCRN8_API_KEY=sk-ant-api03-... in bash. Only rncrn8 reads that variable, so Claude Code keeps using your Claude login' + ". ANTHROPIC_API_KEY or `ant auth login` also work, but Claude Code reads ANTHROPIC_API_KEY "
    "too and, once approved, bills its own use to it instead of a Claude subscription."
)


EXPECTED = (ValueError, KeyError, FileNotFoundError, AdapterError, ModelError, LockTimeout)


def _plain(fn):
    """A failure a tool sees coming reaches the assistant as its message. The SDK shows any other exception
    as "Error executing tool", which would hide the very thing the user needs to know, such as where to set
    the model key."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except EXPECTED as exc:
            message = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
            raise ToolError(str(message)) from exc

    return wrapper


def _need_key(settings: Any) -> None:
    from .models.claude import credentials_found

    if settings.models.adapter == "claude" and not credentials_found():
        raise ValueError(KEY_MISSING)


class Answer(BaseModel):
    """One finding the user answered."""

    decision_id: str = Field(description="the finding's decision_id, from `review` or `status`")
    right: bool = Field(description="whether the finding turned out right")
    reason: str = Field(description="why, in the user's own words")
    source: Literal["user", "check", "loop"] = Field(
        description="who stands behind it: 'user' when the user said so, 'check' when you ran a check and showed the user "
        "the result, 'loop' when it comes from an automated loop or an agent with nobody confirming it. Only 'user' and "
        "'check' teach the track record; 'loop' is recorded but moves no trust")
    fact: str | None = Field(default=None, description="something the user said the panel should know from now on, as one self-contained sentence")


WEIGHTS_NOTE = (
    "weight is how much a persona's confidence counts in the panel's combined belief; weights start equal and add up to 1. "
    "It follows score, not right_side: a confident call that turns out right raises it more than a hesitant one, and a "
    "confident miss lowers it more. score is the average Brier score of the persona's readings: 0 is perfect, 0.25 is what "
    "saying 50% every time would get, lower is better.")


def _persona_view(created: Any, settings: Any) -> dict[str, Any]:
    p = created.persona
    out = {"name": p.name, "display_name": p.display_name, "reviews": p.applies_to, "raises_above": round(p.loss_for(settings).threshold, 2),
           "joined_panels": created.joined, "asked_for_name": created.asked_for}
    return {k: v for k, v in out.items() if v not in ([], "")}


def _project_dir(workspace: Path | None) -> Path:
    if workspace is not None:
        return workspace
    named = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(named) if named and Path(named).is_dir() else Path.cwd()


def _user_name(project: Path) -> str:
    return git(project, "config", "user.name") or "the user"


def build_server(workspace: Path | None = None) -> MCPServer:
    server = MCPServer("rncrn8", instructions=INSTRUCTIONS)

    def ws() -> Workspace:
        where = _project_dir(workspace)
        found = Workspace.find(where)
        if found is None:
            raise ValueError(f"no .rncrn8 workspace at or above {where}; call `setup` to create one, or run `rncrn8 init` in the project folder")
        return found

    def entry_dict(e) -> dict[str, Any]:
        return {
            "id": e.id,
            "persona": e.persona,
            "claim": e.claim,
            "source": e.source.value,
            "origin": e.origin,
            "who": e.who,
            "date": e.date.isoformat(),
            "confidence": round(e.confidence, 3),
            "tags": e.tags,
            "stale": e.is_stale(),
        }

    # --- reviews ----------------------------------------------------------------------------

    @server.tool(description=(
        "Have the rncrn8 panel review something, and get a digest to put in plain words for the user. Give one of: diff, a git "
        "range such as 'main..HEAD', 'HEAD' for uncommitted changes or 'staged'; path, a file, .patch, video folder or packet "
        "JSON; text, a plan or pitch. With none, the current branch is reviewed against its base. paths narrows a diff to files "
        "or folders. Makes model calls and takes a minute or more. Each finding carries the decision_id that `answer` takes."))
    @_plain
    def review(path: str | None = None, diff: str | None = None, text: str | None = None, kind: str | None = None, panel: list[str] | None = None,
               debate: bool = True, summaries: bool = True, max_findings: int | None = None, repo: str | None = None, paths: list[str] | None = None,
               full_report: bool = False) -> dict[str, Any]:
        from .onboard import default_range

        w = ws()
        given = [x for x in (path, diff, text) if x]
        if len(given) > 1:
            raise ValueError("give one of path, diff or text, not several")
        repo_path = Path(repo) if repo else w.project_dir
        if not given:
            diff = default_range(repo_path)
            if diff is None:
                raise ValueError("nothing to review by default here (no git history); give a diff, a path or a text")
        try:
            if diff:
                packet = git_diff_packet(repo_path, diff, paths=paths)
            elif text:
                packet = text_packet(text, "text", kind or "idea")
            else:
                target = Path(path)
                if not target.is_absolute():  # the project's path, whatever folder the server was started in
                    target = w.project_dir / target
                packet = build_packet(target, kind=kind)
        except (AdapterError, FileNotFoundError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        cfg = w.load_config()
        if packet.reviewed_chars > cfg.review.max_packet_chars:
            from .packet import size_by_path

            folders, files = size_by_path(packet)
            by_folder = "; ".join(f"{p} {n:,}" for p, n in folders[:12]) or "none"
            by_file = "; ".join(f"{p} {n:,}" for p, n in files[:10])
            raise ValueError(
                f"target is {packet.reviewed_chars:,} characters, above review.max_packet_chars ({cfg.review.max_packet_chars:,}), so it was not reviewed. "
                f"Characters by folder: {by_folder}. Largest files: {by_file}. Review it in parts that each fit, by giving paths: folders "
                "and files can be mixed, and ':(exclude)path' leaves one out. Each part is its own review, with its own model calls.")
        _need_key(cfg)
        adapter = make_adapter(None, cfg.models)
        out = run_review(w, packet, adapter, panel=panel or None, debate=debate, summaries=summaries, max_findings=max_findings, target=path or (f"diff:{diff}" if diff else "text"))
        result = digest(out.review, out.review_dir)
        if full_report:
            result["report_markdown"] = render_report(out.review)
        return result

    @server.tool(description=(
        "Record what the user said about findings: for each one they answered, whether it turned out right, and why, in their "
        "words. Pass every finding the user answered in one call, one entry each; decision_id comes from `review` or `status`. "
        "When the user states something the panel should know from now on, pass it as fact on that entry, one self-contained "
        "sentence: it is kept as something the user said, for every persona. Record only the user's word or a check you ran and "
        "showed them, never your own opinion. Trust moves in every witness behind each finding. The result lists what was "
        "recorded, any answer that could not be and why, and the findings of those reviews still open."))
    @_plain
    def answer(answers: list[Answer], who: str | None = None) -> dict[str, Any]:
        if not answers:
            raise ValueError("no answers given: pass one entry for each finding the user answered")
        w = ws()
        person = who or _user_name(w.project_dir)
        recorded: list[dict[str, Any]] = []
        not_recorded: list[dict[str, str]] = []
        reviews: list[str] = []
        weights: dict[str, float] = {}
        for a in answers:
            try:
                res = record_outcome(w, a.decision_id, a.right, note=a.reason, source=FROM_CALLER[a.source])
            except EXPECTED as exc:  # one wrong id must not cost the user the rest of their answers
                not_recorded.append({"decision_id": a.decision_id, "why": str(exc.args[0] if isinstance(exc, KeyError) and exc.args else exc)})
                known = MemoryStore(w).get_decision(a.decision_id)
                if known is not None and known.review_id and known.review_id not in reviews:
                    reviews.append(known.review_id)
                continue
            kept = None
            if a.fact and a.fact.strip():
                kept = remember_service(w, SHARED_PERSONA, a.fact.strip(), SourceType.HUMAN, f"answer to {res.record.id}", who=person, tags=["answer"])
            recorded.append({
                "decision_id": res.record.id,
                "recorded": "confirmed" if a.right else "refuted",
                "claim": res.record.claim,
                "reason": a.reason,
                "source": a.source,
                "teaches": teaches(res.record.outcome_source),
                "replaced_unconfirmed": res.replaced,
                "fact_kept": entry_dict(kept) if kept else None,
                "trust_moved": [{"memory": e.id, "origin": e.origin, "confidence": round(e.confidence, 2)} for e in res.updated],
                "memories_linked": len(res.linked or []),
            })
            weights = res.panel_weights or weights
            if res.record.review_id and res.record.review_id not in reviews:
                reviews.append(res.record.review_id)
        # still open: no outcome yet, or only an unconfirmed one that a person's word can still replace
        still_open = [r for r in MemoryStore(w).load_decisions() if r.review_id in reviews and not r.withdrawn and (r.outcome is None or not teaches(r.outcome_source))]
        return {
            "recorded": recorded,
            "not_recorded": not_recorded,
            "still_open": [{"decision_id": r.id, "persona": r.persona, "kind": r.kind, "where": r.where, "claim": r.claim, "review_id": r.review_id} for r in still_open],
            "panel_weights": {k: round(v, 2) for k, v in weights.items()},
        }

    @server.tool(description="Close open code findings from a review of a git range whose cited lines changed since the reviewed commit. Call it after changes to reviewed code are committed.")
    @_plain
    def followup(review_id: str, to: str = "HEAD", dry_run: bool = False) -> dict[str, Any]:
        from .followup import followup as run_followup

        res = run_followup(ws(), review_id, to=to, dry_run=dry_run)
        return {"review_id": res.review_id, "base": res.base, "top": res.top, "notes": res.notes,
                "items": [{"decision_id": i.decision_id, "persona": i.persona, "where": i.where, "claim": i.claim, "closed": i.touched and i.recorded, "note": i.note} for i in res.items]}

    @server.tool(description="Where things stand, without model calls: findings still waiting for an outcome, recent reviews, each persona's track record, and whether the model key is set.", annotations=READ_ONLY)
    @_plain
    def status(limit: int = 30) -> dict[str, Any]:
        from .track import PanelState, reading_points

        w = ws()
        cfg = w.load_config()
        recs = MemoryStore(w).load_decisions()
        waiting = [r for r in recs if r.outcome is None and not r.withdrawn]
        points = reading_points(recs)
        panel_state = PanelState.load(w.panel_path)
        need = cfg.calibration.min_points
        people = []
        for p in list_personas(w):
            n = len(points.get(p.name, []))
            row: dict[str, Any] = {"name": p.name, "display_name": p.display_name, "reviews": p.applies_to,
                                   "calibration": f"calibrated on {n} outcomes" if n >= need else f"taken at its word until {need} outcomes recorded by a person; has {n}"}
            if p.name in panel_state.weights:
                k = panel_state.n.get(p.name, 0)
                row.update(weight=round(panel_state.weights[p.name], 2), outcomes=k, right_side=f"{panel_state.right.get(p.name, 0)}/{k}")
                score = panel_state.mean_brier(p.name)
                if score is not None:
                    row["score"] = round(score, 2)
            people.append(row)
        reviews = list_reviews(w)[-5:][::-1]
        from .models.claude import credentials_found

        from .memory.legacy import git_standing

        out = {
            "workspace": str(w.root),
            "workspace_in_git": git_standing(w.project_dir, w.root.name),
            "model_key": "found" if credentials_found() else "missing: reviews cannot run",
            "open_count": len(waiting),
            "open_findings": [{"decision_id": r.id, "persona": r.persona, "kind": r.kind, "where": r.where, "claim": r.claim, "raised": r.raise_finding, "review_id": r.review_id} for r in waiting[:limit]],
            "recent_reviews": [{"review_id": r.id, "when": r.created_at.strftime("%Y-%m-%d %H:%M UTC"), "title": r.title, "counts": digest(r)["counts"]} for r in reviews],
            "personas": people,
        }
        if any("weight" in row for row in people):
            out["weights_note"] = WEIGHTS_NOTE
        return out

    # --- setup ----------------------------------------------------------------------------------

    @server.tool(description=(
        "Read this project and propose how to set rncrn8 up for it: what every persona should know, each line with the file that "
        "says it; the decisions the files state; lines left out and why; and a panel fitted to the project. The first call makes "
        "one model call and keeps the proposal; later calls return it without cost, unless refresh is true. Show it to the user in "
        "plain words, ask, then call `setup_apply` with their answers."))
    @_plain
    def setup(refresh: bool = False) -> dict[str, Any]:
        from . import onboard
        from .persona import TEMPLATES

        root = onboard.project_root(_project_dir(workspace))
        w = Workspace.find(root)
        if w is None or not w.project_dir.samefile(root):
            w = Workspace.init(root)
        onboard.exclude_from_git(root)
        kept = None if refresh else onboard.load_proposal(w)
        fresh = kept is None
        if fresh:
            cfg = w.load_config()
            _need_key(cfg)
            model = onboard.CountingAdapter(make_adapter(None, cfg.models))
            scan = onboard.scan_project(root)
            proposal = onboard.propose(model, scan, [p.name for p in list_personas(w)], cfg.sources.max_facts, cfg.models.max_tokens, decided=onboard.recorded_decision_claims(w))
            onboard.save_proposal(w, proposal, scan, {"model_calls": model.calls, "tokens_in": model.input_tokens, "tokens_out": model.output_tokens})
            kept = onboard.load_proposal(w)
        proposal, meta = kept
        world = [{"kind": kind, "text": s.text, "source": s.source, **({"decided": s.when.isoformat()} if s.when else {})}
                 for kind, items in (("project", proposal.project), ("constraint", proposal.constraints), ("decision", proposal.decisions)) for s in items]
        panel = [{"name": n, "display_name": TEMPLATES[n].display_name, "why": why, "new": False} for n, why in proposal.templates if n in TEMPLATES]
        panel += [{"name": b.name, "display_name": b.display_name, "role": b.role, "why": b.why, "new": True} for b in proposal.new_personas]
        return {
            "project": proposal.name or root.name,
            "summary": proposal.summary,
            "kinds": proposal.kinds,
            "world": world,
            "left_out": proposal.rejected,
            "panel": panel,
            "default_panels": proposal.panels,
            "already_in_workspace": [p.name for p in list_personas(w)],
            "not_found_in_files": proposal.notes,
            "files_read": len(meta["files_read"]),
            "files_left_out": meta["files_left_out"],
            "cost": meta["cost"] if fresh else "kept from an earlier call; nothing new spent",
        }

    @server.tool(description=(
        "Apply the kept setup proposal with the user's answers. world: whether to write what the panel should know and the "
        "decisions. personas: the names to create; leave it out to create every one proposed. add: more personas as 'name: role' "
        "lines, written for this project. history: whether to read the git log into memory. Makes model calls for new personas "
        "and the history. Running it again adds only what is new."))
    @_plain
    def setup_apply(world: bool = True, personas: list[str] | None = None, add: list[str] | None = None, history: bool = True) -> dict[str, Any]:
        from . import onboard

        root = onboard.project_root(_project_dir(workspace))
        w = Workspace.find(root)
        kept = onboard.load_proposal(w) if w is not None else None
        if kept is None:
            raise ValueError("no setup proposal is kept for this project; call `setup` first")
        proposal, _ = kept
        cfg = w.load_config()
        chosen = None if personas is None else {onboard.slug(n) for n in personas}
        writes_personas = bool(add) or any(chosen is None or b.name in chosen for b in proposal.new_personas)
        if writes_personas or history:
            _need_key(cfg)
        model = onboard.CountingAdapter(make_adapter(None, cfg.models))
        r = onboard.apply_setup(w, root, onboard.scan_project(root), proposal, model, cfg, world=world, chosen=chosen, extra=list(add or []), history=history)
        return {
            "world_written": r.world.written if r.world else [],
            "world_kept_as_written": r.world.kept if r.world else [],
            "decisions_recorded": len(r.world.decisions) if r.world else 0,
            "decisions_already_recorded": r.world.already if r.world else 0,
            "facts_from_files": r.facts_added,
            "facts_from_history": r.history_facts,
            "history_problem": r.history_error,
            "personas_created": [_persona_view(c, cfg) for c in r.created + r.added],
            "default_panels": w.load_config().review.panels,
            "problems": r.problems,
            "cost": {"model_calls": model.calls, "tokens_in": model.input_tokens, "tokens_out": model.output_tokens},
            "next": "The panel is ready: ask for a review, for example 'review my branch', or type /rncrn8:review.",
        }

    @server.tool(description="Write one new persona for this project from 'name: role' (a role alone works too); it joins every default panel it fits. One model call.")
    @_plain
    def add_persona(line: str) -> dict[str, Any]:
        from . import onboard

        w = ws()
        cfg = w.load_config()
        _need_key(cfg)
        kept = onboard.load_proposal(w)
        proposal = kept[0] if kept else onboard.proposal_from_world(w)
        made, problems = onboard.add_persona(w, proposal, make_adapter(None, cfg.models), cfg.models.max_tokens, line)
        return {"created": [_persona_view(c, cfg) for c in made], "problems": problems}

    # --- ready-made personas ----------------------------------------------------------------------

    @server.tool(description="The ready-made personas rncrn8 ships, by what they read, with the question each asks first, the preset panels, and which are already in this project. No model calls.", annotations=READ_ONLY)
    @_plain
    def seed_personas() -> dict[str, Any]:
        from .persona import GROUPS, PANELS, TEMPLATES

        have = {p.name for p in list_personas(ws())}
        return {
            "groups": [{"reads": label, "personas": [{"name": n, "display_name": TEMPLATES[n].display_name, "reviews": TEMPLATES[n].applies_to,
                                                      "asks_first": TEMPLATES[n].rubric[0], "in_this_project": n in have} for n in names]}
                       for label, names in GROUPS],
            "panels": {k: v for k, v in PANELS.items() if k != "all"},
        }

    @server.tool(description=(
        "Add ready-made personas to this project, by name, or as a preset panel: code, pitch (ideas, plans and strategies), document or "
        "video. Personas already here are left as they are. A preset becomes the default panel for its kind of review when that kind "
        "has none yet; a persona added by name joins the default panels it fits. No model calls."))
    @_plain
    def add_seed_personas(names: list[str] | None = None, panel: str | None = None) -> dict[str, Any]:
        from . import onboard
        from .persona import PANELS, TEMPLATES, scaffold_panel, scaffold_persona
        from .persona.loader import persona_path

        w = ws()
        if not names and not panel:
            raise ValueError("give names of ready-made personas, a preset panel (code, pitch, document, video), or both")
        unknown = [n for n in names or [] if n not in TEMPLATES]
        if unknown:
            raise ValueError(f"no ready-made persona named {', '.join(unknown)}; there are {', '.join(TEMPLATES)}")
        if panel is not None and panel not in PANELS:
            raise ValueError(f"no preset panel '{panel}'; there are {', '.join(PANELS)}")
        made, here = [], []
        if panel:
            created, existing = scaffold_panel(w, panel)
            made += [onboard.Created(p) for p in created]
            here += existing
        for n in dict.fromkeys(names or []):
            if persona_path(w, n).exists():
                if n not in here and all(c.persona.name != n for c in made):
                    here.append(n)
                continue
            p = scaffold_persona(w, n, template=n)
            made.append(onboard.Created(p, onboard.join_panels(w, p)))
        kinds = {"code": ["code"], "pitch": ["idea"], "document": ["document"], "video": ["video"]}.get(panel or "", [])
        set_now = onboard.save_panels(w, {kind: PANELS[panel] for kind in kinds}) if kinds else {}
        cfg = w.load_config()
        return {"created": [_persona_view(c, cfg) for c in made], "already_here": here, "default_panels_set": set_now, "default_panels": cfg.review.panels}

    # --- memory from outside ---------------------------------------------------------------------

    @server.tool(description=(
        "Send one persona to the web to research a question through its own lens. It searches at most budget times and remembers "
        "what it found as internet memories, each with its URL, who published it and when; with shared true, every persona gets "
        "them. Makes model calls with web search, which cost more than a review call: ask the user first."))
    @_plain
    def research(persona: str, question: str, budget: int | None = None, shared: bool = False) -> dict[str, Any]:
        from .sources.research import research as run_research

        w = ws()
        cfg = w.load_config()
        _need_key(cfg)
        res = run_research(w, make_adapter(None, cfg.models), persona, question, budget=budget or cfg.sources.research_budget,
                           max_facts=cfg.sources.max_facts, shared=shared)
        return {
            "persona": persona,
            "remembered_by": "every persona" if shared else persona,
            "found": [{"claim": e.claim, "source": e.origin, "who": e.who, "date": e.date.isoformat(), "confidence": round(e.confidence, 2)} for e in res.recorded],
            "already_known": res.skipped_duplicates,
            "notes": res.notes,
            "pages_read": len(res.sources),
            "problem": res.error,
        }

    @server.tool(description=(
        "Record notes or a transcript of what a person said, from a call, an interview or a meeting, as memories attributed to them. "
        "Give text, the notes themselves, or path, a file in the project; who said it; origin, where it was said, such as 'call with a "
        "clinic owner 2026-11-10'; and date if known (YYYY-MM-DD). Each fact keeps who said it, so personas weigh it as a person's word. "
        "persona is 'shared' for every persona, or one persona's name. One model call."))
    @_plain
    def ingest_notes(who: str, origin: str, text: str | None = None, path: str | None = None, date: str | None = None, persona: str = "shared") -> dict[str, Any]:
        from datetime import date as _date

        from .sources.notes import ingest_notes as run_notes

        w = ws()
        cfg = w.load_config()
        _need_key(cfg)
        file = None
        if path:
            file = Path(path) if Path(path).is_absolute() else w.project_dir / path
        res = run_notes(w, make_adapter(None, cfg.models), file, who=who, origin=origin, persona=persona,
                        when=_date.fromisoformat(date) if date else None, max_facts=cfg.sources.max_facts, text=text)
        return {
            "who": who,
            "origin": origin,
            "remembered_by": "every persona" if persona == SHARED_PERSONA else persona,
            "facts": [{"claim": e.claim, "who": e.who, "date": e.date.isoformat()} for e in res.recorded],
            "already_known": res.skipped_duplicates,
            "notes": res.notes,
            "problem": res.error,
        }

    # --- memory and decisions -----------------------------------------------------------------

    @server.tool(description="Record a settled decision in world.md and shared memory, so personas treat it as settled and raise doubts about it only as questions.")
    @_plain
    def decide(claim: str, who: str, origin: str, why: str | None = None, date: str | None = None) -> dict[str, Any]:
        from datetime import date as _date

        from .service import find_decision

        existing = find_decision(ws(), claim)
        if existing is not None:
            return {**entry_dict(existing), "already_recorded": True}
        e = decide_service(ws(), claim, who=who, origin=origin, when=_date.fromisoformat(date) if date else None, why=why)
        return entry_dict(e)

    @server.tool(description="Give a persona (or 'shared') a memory with its provenance. source is one of project, history, internet, human, outcome, inference.")
    @_plain
    def remember(persona: str, claim: str, source: str, origin: str, who: str | None = None, confidence: float | None = None, tags: list[str] | None = None, date: str | None = None) -> dict[str, Any]:
        from datetime import date as _date

        observed = _date.fromisoformat(date) if date else None
        e = remember_service(ws(), persona, claim, SourceType(source), origin, who=who, observed=observed, confidence=confidence, tags=tags or [])
        return entry_dict(e)

    @server.tool(description="What a persona remembers (its own memory plus the shared store), with provenance. Use persona 'shared' for the shared store.", annotations=READ_ONLY)
    @_plain
    def recall(persona: str, query: str | None = None, tag: str | None = None, limit: int = 40) -> list[dict[str, Any]]:
        entries = MemoryStore(ws()).search(persona, query=query, tags=[tag] if tag else None)
        return [entry_dict(e) for e in entries[-limit:]]

    @server.tool(description="List the personas in this workspace with what they review and how they weigh mistakes.", annotations=READ_ONLY)
    @_plain
    def personas() -> list[dict[str, Any]]:
        w = ws()
        store = MemoryStore(w)
        cfg = w.load_config()
        out = []
        for p in list_personas(w):
            loss = p.loss_for(cfg)
            out.append({"name": p.name, "display_name": p.display_name, "applies_to": p.applies_to, "memories": len(store.load(p.name)), "false_alarm_cost": loss.false_alarm, "miss_cost": loss.miss, "character": p.character})
        return out

    @server.tool(description="List recorded decisions (findings), open ones by default, with their full claims and ids.", annotations=READ_ONLY)
    @_plain
    def decisions(open_only: bool = True, review_id: str | None = None, persona: str | None = None) -> list[dict[str, Any]]:
        recs = MemoryStore(ws()).load_decisions(persona)
        if open_only:
            recs = [r for r in recs if r.outcome is None and not r.withdrawn]
        if review_id:
            recs = [r for r in recs if r.review_id == review_id]
        return [{"id": r.id, "persona": r.persona, "claim": r.claim, "where": r.where, "kind": r.kind, "belief": round(r.belief, 3), "raised": r.raise_finding, "review_id": r.review_id, "outcome": r.outcome} for r in recs]

    @server.tool(description="Lower-level form of `answer`: record whether a finding turned out right, with an optional note, and keep no fact. decision_id is a d-... id or r-<review>:<persona>:F<n>. source is 'user', 'check' or 'loop', as on `answer`; only 'user' and 'check' teach.")
    @_plain
    def outcome(decision_id: str, confirmed: bool, source: Literal["user", "check", "loop"], note: str | None = None) -> dict[str, Any]:
        res = record_outcome(ws(), decision_id, confirmed, note, source=FROM_CALLER[source])
        return {"decision_id": res.record.id, "claim": res.record.claim, "outcome": res.record.outcome, "source": source,
                "teaches": teaches(res.record.outcome_source), "replaced_unconfirmed": res.replaced,
                "updated_memories": [e.id for e in res.updated], "memories_linked": len(res.linked or []), "panel_weights": res.panel_weights}

    # --- slash commands ------------------------------------------------------------------------

    @server.prompt(name="setup", title="Set up rncrn8 for this project",
                   description="Read the project, propose what the panel should know and who sits on it, and set it up with your answers.")
    def setup_prompt() -> str:
        return ("Set up rncrn8 for this project. Call the rncrn8 `setup` tool, then show me in plain words: what the panel will be told about the "
                "project, the decisions it found and which file says each, anything it left out and why, and the panel it suggests, with one line "
                "on why each persona fits. Ask me whether to go ahead, whether to drop anyone, and whether I want anyone else on the panel; a name "
                "and a role is enough. Then call `setup_apply` with my answers, and tell me what was created and how to ask for a first review.")

    @server.prompt(name="review", title="Review with the panel",
                   description="Have the rncrn8 panel review your work and hear what it found in plain words. Optional: a git range such as main..HEAD, or a file path; default is this branch against its base.")
    def review_prompt(target: str = "") -> str:
        what = f"`{target}`" if target else "my current branch against its base"
        how = (f"with `{target}` as the diff if it is a git range or as the path if it is a file" if target else "with no target")
        return (f"Have the rncrn8 panel review {what}: call the rncrn8 `review` tool {how}. Tell me first that it takes a minute or more. "
                "Then tell me what the panel found, in plain words: the problems it agrees on first, then disputed findings and how to settle them, "
                "then the panel's questions: answer from the code those it can settle, saying what you checked, and ask me the rest. "
                "Keep it short, and end with the next step you can take for me.")

    @server.prompt(name="outcomes", title="Settle open findings",
                   description="Go through the panel's open findings one at a time and record what turned out true.")
    def outcomes_prompt() -> str:
        return ("Go through the open rncrn8 findings with me, one at a time: call the rncrn8 `status` tool to get them. For each, tell me in one or two "
                "plain sentences what it claims and where, and ask whether it turned out right; when a quick check would settle it, offer to run it. "
                "Record each answer with `answer`, my reason in my words, as `fact` anything I say the panel should know from now on, and any "
                "rule I state with `decide`. "
                "Stop when none are left or when I say so.")

    @server.prompt(name="status", title="How the panel is doing",
                   description="What is still open, and how each persona's track record looks, in plain words.")
    def status_prompt() -> str:
        return ("Call the rncrn8 `status` tool and tell me in plain words what is still open and how each persona's track record looks so far. "
                "When you mention weights, say what they follow, as its weights_note explains: how close each persona's confidence was to "
                "what happened, not only how often it was right.")

    return server


def main(workspace: Path | None = None) -> None:
    build_server(workspace).run("stdio")
