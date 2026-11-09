"""Command line. Thin: parse, call service, print."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import click
import yaml

from . import __version__
from .config import EngineConfig, SourceType
from .memory import MemoryEntry, MemoryStore, Workspace
from .memory.store import SHARED_PERSONA
from .adapters import AdapterError, build_packet, git_diff_packet, text_packet
from .models import ADAPTERS, DryRunAdapter, ModelError, make_adapter
from .packet import KINDS
from .persona import GROUPS, PANELS, TEMPLATES, list_personas, load_persona, scaffold_panel, scaffold_persona
from .persona.loader import InvalidPersonaFile
from .review import load_review, run_review
from .review.report import render_report
from .review.runner import list_reviews
from .service import judge as judge_service
from .service import record_decision as decide_service
from .service import record_outcome
from .service import remember as remember_service
from .track import PanelState, fit_calibrators, reading_points


def _fmt_or_na(x: float, places: int) -> str:
    """A metric for a table cell; NaN or infinity prints as n/a instead of a value that looks like a bug."""
    return f"{x:.{places}f}" if math.isfinite(x) else "n/a"


def _renamed(ws: Workspace | None) -> Workspace | None:
    """Say once, on stderr, that finding the workspace renamed it from its old name."""
    if ws is not None and ws.renamed_from is not None:
        click.echo(f"renamed {ws.renamed_from} to {ws.root}: the tool is now called rncrn8, and everything recorded in the workspace is unchanged", err=True)
    return ws


def _ws() -> Workspace:
    ws = _renamed(Workspace.find())
    if ws is None:
        raise click.ClickException("no .rncrn8 workspace here or above; `rncrn8 init` in the project folder creates one and connects your assistant")
    return ws


def _require_persona(ws: Workspace, name: str) -> None:
    if name != SHARED_PERSONA and not (ws.persona_dir(name) / "persona.yaml").exists():
        raise click.ClickException(f"no persona named '{name}'; see `rncrn8 persona list`")


def _fmt_entry(e: MemoryEntry, verbose: bool = False) -> str:
    stale = f" STALE (re-check; review date {e.expires_at.isoformat()} passed)" if e.is_stale() else ""
    head = f"{e.id}  [{e.source.value:9}] conf={e.confidence:.2f} (n={e.reliability.strength:.1f}){stale}"
    body = f"    {e.claim}"
    when = f"seen {e.date.isoformat()}, publication date unknown" if "date:unknown" in e.tags else e.date.isoformat()
    prov = f"    from: {e.origin}" + (f" | who: {e.who}" if e.who else "") + f" | {when}"
    lines = [head, body, prov]
    if e.tags:
        lines.append(f"    tags: {', '.join(e.tags)}")
    if verbose:
        if e.root:
            lines.append(f"    root: {e.root}")
        if e.confirmed_by or e.contradicted_by:
            lines.append(f"    confirmed by {len(e.confirmed_by)}, contradicted by {len(e.contradicted_by)}")
        if e.note:
            lines.append(f"    note: {e.note}")
    return "\n".join(lines)


class _Group(click.Group):
    """Turns a bad config.yaml or persona.yaml into a one-line error instead of a traceback."""

    def invoke(self, ctx):
        from pydantic import ValidationError

        try:
            return super().invoke(ctx)
        except ValidationError as exc:
            where = "config.yaml" if exc.title == "EngineConfig" else ("a persona.yaml" if exc.title == "Persona" else exc.title)
            problems = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:5])
            raise click.ClickException(f"cannot read {where}: {problems}")
        except yaml.YAMLError as exc:
            raise click.ClickException(f"a YAML file in .rncrn8 is not valid: {exc}")
        except InvalidPersonaFile as exc:
            raise click.ClickException(str(exc))


@click.group(cls=_Group, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="rncrn8")
def main() -> None:
    """Personas with separated, provenance-tagged memory."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")  # never crash on a character the console cannot show
        except (AttributeError, ValueError):
            pass


# --- init ---------------------------------------------------------------------------


@main.command()
@click.argument("path", type=click.Path(file_okay=False, path_type=Path), default=".")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="connect Claude Code without asking")
@click.option("--no-connect", is_flag=True, help="only create the workspace; connect no assistant")
def init(path: Path, assume_yes: bool, no_connect: bool) -> None:
    """Create the workspace in PATH (default: here) and connect rncrn8 to Claude Code, so that everything after this happens in the chat."""
    from . import connect, onboard

    ws = _renamed(Workspace.init(path))
    click.echo(f"workspace: {ws.root.resolve()}")
    if onboard.exclude_from_git(ws.project_dir):
        click.echo("  kept .rncrn8/ out of git through .git/info/exclude; delete that line to commit the workspace instead")
    connected = False
    if not no_connect:
        claude = connect.find_claude()
        if claude is None:
            click.echo("Claude Code was not found on this PATH. To connect an assistant later:")
            for line in connect.manual_steps(ws.project_dir):
                click.echo(f"  {line}")
        elif connect.registered(claude):
            connected = True
            click.echo("Claude Code: rncrn8 is already connected")
        elif _ask_or_no("connect rncrn8 to Claude Code, for all your projects?", assume_yes):
            done, said = connect.register(claude)
            if done:
                connected = True
                click.echo("Claude Code: connected for all your projects, in the terminal and the VS Code extension; `claude mcp remove rncrn8 -s user` disconnects it")
            else:
                click.echo(f"Claude Code did not take the connection: {said}")
                for line in connect.manual_steps(ws.project_dir):
                    click.echo(f"  {line}")
        else:
            click.echo("not connected; `rncrn8 init` asks again, or connect by hand:")
            for line in connect.manual_steps(ws.project_dir):
                click.echo(f"  {line}")
    from .models.claude import credentials_found

    if not credentials_found():
        click.echo("No Anthropic key found. Reviews need one where Claude Code runs: " + 'setx RNCRN8_API_KEY "sk-ant-api03-..." in PowerShell, then restart VS Code or the terminal Claude Code runs in; export RNCRN8_API_KEY=sk-ant-api03-... in bash. Only rncrn8 reads that variable, so Claude Code keeps using your Claude login')
    ask = "ask for a review, or type /rncrn8:review" if list_personas(ws) else "type /rncrn8:setup, or ask it to set up rncrn8"
    if connected:
        click.echo(f"next: open Claude Code in this project and {ask}")
    else:
        click.echo(f"next: connect Claude Code, with `rncrn8 init` again or the command above, then open it in this project and {ask}")


@main.command()
@click.option("--live", is_flag=True, help="also make one tiny model call to check the key and the model; costs a fraction of a cent")
@click.option("--adapter", type=click.Choice(ADAPTERS), default=None, help="adapter for the live check; default from config.yaml")
def doctor(live: bool, adapter: str | None) -> None:
    """Check the install, the shell and this workspace, one line each, and say what to do about anything wrong."""
    from .doctor import run_checks

    ws = _renamed(Workspace.find())
    checks = run_checks(ws, live=live, adapter_name=adapter)
    for c in checks:
        click.echo(f"{c.level:<5} {c.name:<13} {c.detail}")
    fails = [c for c in checks if c.level == "fail"]
    warns = [c for c in checks if c.level == "warn"]
    if fails:
        click.echo(f"{len(fails)} {'problem' if len(fails) == 1 else 'problems'} above must be fixed before a review will run")
        click.get_current_context().exit(1)
    tail = f", {len(warns)} {'warning' if len(warns) == 1 else 'warnings'} worth reading" if warns else ""
    if ws is None:
        click.echo(f"ready{tail}; next: `rncrn8 init` in the project folder, then ask your assistant to set up rncrn8 (in Claude Code: /rncrn8:setup)")
    elif not list_personas(ws):
        click.echo(f"ready{tail}; next: ask your assistant to set up rncrn8 (in Claude Code: /rncrn8:setup)")
    else:
        click.echo(f"ready{tail}; next: ask your assistant for a review (in Claude Code: /rncrn8:review)")


def _ask(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        click.echo(f"{question} yes (--yes)")
        return True
    try:
        return click.confirm(question, default=True)
    except click.Abort as exc:
        raise click.ClickException("setup asks a few questions; run it in a terminal, or pass --yes to accept every suggestion") from exc


def _ask_or_no(question: str, assume_yes: bool) -> bool:
    """Ask; with no one to answer, the answer is no."""
    if assume_yes:
        click.echo(f"{question} yes (--yes)")
        return True
    try:
        return click.confirm(question, default=True)
    except click.Abort:
        click.echo("")
        return False


def _line(question: str) -> str:
    try:
        return click.prompt(question, default="", show_default=False)
    except click.Abort:
        return ""


def _created(p, extra: str = "") -> str:
    loss = p.loss_for(EngineConfig())
    return f"  created {p.name} ({p.display_name}) · reviews {', '.join(p.applies_to)} · raises above belief {loss.threshold:.2f}{extra}"


@main.command()
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="accept every suggestion without asking")
@click.option("--add", "extra", multiple=True, metavar='"NAME: ROLE"', help="one more persona to write for this project; repeatable")
@click.option("--adapter", type=click.Choice(ADAPTERS), default=None, help="model adapter; default from config.yaml")
@click.option("--no-history", is_flag=True, help="do not read git log")
def setup(assume_yes: bool, extra: tuple[str, ...], adapter: str | None, no_history: bool) -> None:
    """Read this project and set rncrn8 up for it: the shared world, the decisions its files state, facts, and a panel fitted to it."""
    import textwrap

    from . import onboard

    root = onboard.project_root(Path.cwd())
    ws = _renamed(Workspace.find(root))
    if ws is None or not ws.project_dir.samefile(root):
        ws = _renamed(Workspace.init(root))
        click.echo(f"created {ws.root}")
    if onboard.exclude_from_git(root):
        click.echo("  kept .rncrn8/ out of git through .git/info/exclude; delete that line to commit the workspace instead")
    if onboard.workspace_tracked(root, ws.root.name):
        click.echo(f"  {ws.root.name}/ is committed in this repository. Reviews leave it out; `git rm -r --cached {ws.root.name}` stops tracking it and deletes nothing on disk")
    cfg = ws.load_config()
    model = onboard.CountingAdapter(make_adapter(adapter, cfg.models))

    scan = onboard.scan_project(root)
    paths = [f.path for f in scan.files]
    shown = ", ".join(paths[:6]) + (f" and {len(paths) - 6} more" if len(paths) > 6 else "")
    click.echo(f"read {len(paths)} files from {root}: {shown or 'none'}" + (f"; {scan.skipped} left out to stay within the reading budget" if scan.skipped else ""))
    existing = [p.name for p in list_personas(ws)]
    try:
        proposal = onboard.propose(model, scan, existing, cfg.sources.max_facts, cfg.models.max_tokens, decided=onboard.recorded_decision_claims(ws))
    except ModelError as exc:
        raise click.ClickException(f"{exc}\nrun `rncrn8 doctor --live` to check the key and the model") from exc
    onboard.save_proposal(ws, proposal, scan, {"model_calls": model.calls, "tokens_in": model.input_tokens, "tokens_out": model.output_tokens})

    click.echo("")
    click.echo((proposal.name or root.name) + (f" ({', '.join(proposal.kinds)})" if proposal.kinds else ""))
    if proposal.summary:
        click.echo(textwrap.fill(proposal.summary, width=100, initial_indent="  ", subsequent_indent="  "))
    if proposal.has_world or proposal.rejected:
        click.echo("")
        click.echo("world.md")
        for label, items in (("project", proposal.project), ("constraint", proposal.constraints), ("decision", proposal.decisions)):
            for s in items:
                click.echo(f"  {label:<11} {s.text} [{s.source}]")
        for r in proposal.rejected:
            click.echo(f"  left out    {r}")
        if proposal.rejected:
            click.echo('  a line is left out when its file does not say it; if it is true, record it with `rncrn8 decide "..." --who you --origin where`')
    accept_world = proposal.has_world and _ask("write these to world.md and shared memory?", assume_yes)

    picks, briefs = proposal.templates, proposal.new_personas
    chosen: set[str] = set()
    if picks or briefs:
        click.echo("")
        click.echo("panel")
        for name, why in picks:
            click.echo(f"  {name:<15} {TEMPLATES[name].display_name}: {why}")
        for b in briefs:
            click.echo(f"  {b.name:<15} {b.display_name or 'new'}: {b.role}")
        if _ask(f"create these {len(picks) + len(briefs)} personas?", assume_yes):
            chosen = {n for n, _ in picks} | {b.name for b in briefs}
        else:
            chosen = {onboard.slug(x) for x in _line("which ones? names separated by commas, or Enter for none").split(",") if x.strip()}
    more = list(extra)
    while not assume_yes:
        line = _line("add another persona? name: role, or Enter to finish").strip()
        if not line:
            break
        more.append(line)

    result = onboard.apply_setup(ws, root, scan, proposal, model, cfg, world=accept_world, chosen=chosen, extra=more, history=not no_history)
    click.echo("")
    if result.world is not None:
        if result.world.written:
            click.echo(f"  wrote {', '.join(result.world.written)}")
        if result.world.kept:
            click.echo(f"  kept {', '.join(result.world.kept)} as you wrote {'it' if len(result.world.kept) == 1 else 'them'}")
        click.echo(f"  recorded {len(result.world.decisions)} decision(s)" + (f", {result.world.already} already on record" if result.world.already else ""))
    click.echo(f"shared memory: {result.facts_added} facts from the project files" + (f", {result.facts_known} already known" if result.facts_known else ""))
    if result.history_facts is not None:
        click.echo(f"shared memory: {result.history_facts} facts from git history" + (f" (model: {result.history_error})" if result.history_error else ""))
    for c in result.created:
        click.echo(_created(c.persona))
    for kind, names in result.panels.items():
        click.echo(f"default panel for {kind} reviews: {', '.join(names)}")
    for c in result.added:
        joined = f"; joined the default {', '.join(c.joined)} panel" if c.joined else ""
        renamed = f"; named {c.persona.name} because {c.asked_for} was taken" if c.asked_for else ""
        click.echo(_created(c.persona, joined + renamed))
    for problem in result.problems:
        click.echo(f"  {problem}")

    click.echo("")
    if model.name == "dry-run" and not (proposal.has_world or proposal.templates or proposal.new_personas or proposal.facts):
        click.echo("dry run: no model read the files, so only the layout, dependency and git facts were recorded")
    cache = f", {model.cache_read:,} read from cache" if model.cache_read else ""
    click.echo(f"setup used {model.calls} model call(s): {model.input_tokens:,} tokens in, {model.output_tokens:,} out{cache}")
    if proposal.notes:
        click.echo(textwrap.fill(f"not found in the files: {proposal.notes}", width=100, subsequent_indent="  "))
    hint = onboard.review_hint(root)
    click.echo(f"next: `rncrn8 review {hint or '<file>'} --dry-run` shows what each persona would receive; drop --dry-run for the live review")


# --- persona ------------------------------------------------------------------------


@main.group()
def persona() -> None:
    """Create and inspect personas."""


@persona.command("new")
@click.argument("name")
@click.option("--template", "-t", type=click.Choice(sorted(TEMPLATES)), default=None, help="start from a seed persona")
@click.option("--display-name", default=None)
def persona_new(name: str, template: str | None, display_name: str | None) -> None:
    """Scaffold a persona folder with persona.yaml, memory.jsonl and summary.md."""
    ws = _ws()
    try:
        p = scaffold_persona(ws, name, template=template, display_name=display_name)
    except (FileExistsError, KeyError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"created persona '{p.name}' ({p.display_name}) in {ws.persona_dir(p.name)}")
    if template is None:
        click.echo("edit persona.yaml: character, rubric, trust_profile, loss")


@persona.command("list")
def persona_list() -> None:
    """List personas in this workspace."""
    ws = _ws()
    personas = list_personas(ws)
    if not personas:
        click.echo("no personas yet")
        return
    store = MemoryStore(ws)
    name_w = max(12, *(len(p.name) for p in personas))  # a long name must not push its row out of line
    title_w = max(24, *(len(p.display_name) for p in personas))
    for p in personas:
        n = len(store.load(p.name))
        loss = p.loss_for(ws.load_config())
        click.echo(f"{p.name:{name_w}} {p.display_name:{title_w}} memories={n:<4} applies_to={','.join(p.applies_to):<16} loss(fa={loss.false_alarm:g}, miss={loss.miss:g})")


@persona.command("templates")
def persona_templates() -> None:
    """Show the seed personas available to `persona new --template`, grouped by what they read."""
    for title, names in GROUPS:
        click.echo(title)
        for name in names:
            p = TEMPLATES[name]
            fear = f"raises above belief {p.loss.threshold:.2f}" if p.loss else "default loss"
            click.echo(f"  {name:10} {p.display_name:22} reviews {','.join(p.applies_to):<20} {fear}")
            click.echo(f"             {p.character[:110]}...")
    click.echo("panels: " + " · ".join(f"{k}: {', '.join(v)}" for k, v in PANELS.items()))
    click.echo("create one with `rncrn8 persona panel <name>`; any subset works with --panel on a review")


@persona.command("panel")
@click.argument("name", type=click.Choice(list(PANELS)))
def persona_panel(name: str) -> None:
    """Create a recommended panel from the templates: code, pitch, document, video or all. Personas already here are kept."""
    ws = _ws()
    created, existing = scaffold_panel(ws, name)
    for p in created:
        click.echo(f"created persona '{p.name}' ({p.display_name})")
    if existing:
        click.echo("already here: " + ", ".join(existing))
        unreadable = []
        for n in existing:
            try:
                load_persona(ws, n)
            except Exception as exc:  # noqa: BLE001 - any failure to load is the finding here: the file is present and unusable
                unreadable.append(f"{n} ({exc})")
        if unreadable:
            click.echo("present but unreadable, fix or delete before a review: " + "; ".join(unreadable))
    n = len(PANELS[name])
    click.echo(f"panel '{name}': {', '.join(PANELS[name])}")
    click.echo(f"{n} personas: a review makes {n} review calls, {n} debate calls (each answering the other {n - 1}) and {n} summary calls; cost grows with the panel, and faster in the debate. Trim with --panel on a review.")


# --- remember / recall ----------------------------------------------------------------


@main.command()
@click.argument("persona_name", metavar="PERSONA")
@click.argument("memory_id")
@click.option("--why", required=True, help="kept with the entry, for the record")
def retire(persona_name: str, memory_id: str, why: str) -> None:
    """Retire a memory PERSONA can see: it stays in the file with the reason and is never shown or cited again."""
    from .service import retire_memory

    ws = _ws()
    _require_persona(ws, persona_name)
    try:
        e, line_removed = retire_memory(ws, persona_name, memory_id, why)
    except (KeyError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"retired {e.id} ({e.persona}): {e.claim[:120]}")
    if "decision" in e.tags and line_removed:
        click.echo("  it was a decision; its line under ## Decisions in world.md was taken out too")
    elif "decision" in e.tags:
        click.echo(f"  it was a decision, but no line under ## Decisions in {ws.world_path} states it exactly; "
                   "if one is still there, edited by hand, take it out yourself")


@main.command()
@click.argument("persona_name", metavar="PERSONA")
@click.option("--claim", required=True, help="the statement being remembered")
@click.option("--source", required=True, type=click.Choice([s.value for s in SourceType]), help="provenance class")
@click.option("--origin", required=True, help="where exactly: URL, 'call with team lead', 'git log', 'README.md'")
@click.option("--who", default=None, help="who said it")
@click.option("--date", "observed", type=click.DateTime(formats=["%Y-%m-%d"]), default=None, help="when observed/published (YYYY-MM-DD)")
@click.option("--confidence", type=click.FloatRange(0.0, 1.0), default=None, help="override the prior mean for this entry")
@click.option("--tag", "tags", multiple=True)
@click.option("--root", default=None, help="shared root reference for entries that trace to the same study/post/review")
@click.option("--note", default=None)
@click.option("--expires", type=click.DateTime(formats=["%Y-%m-%d"]), default=None, help="stale after this date")
def remember(persona_name, claim, source, origin, who, observed, confidence, tags, root, note, expires) -> None:
    """Record a memory for PERSONA (use 'shared' for the store every persona reads)."""
    ws = _ws()
    try:
        e = remember_entry(ws, persona_name, claim, source, origin, who, observed, confidence, list(tags), root, note, expires)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(_fmt_entry(e, verbose=True))


def remember_entry(ws, persona_name, claim, source, origin, who, observed, confidence, tags, root, note, expires) -> MemoryEntry:
    return remember_service(
        ws,
        persona_name,
        claim,
        SourceType(source),
        origin,
        who=who,
        observed=observed.date() if observed else None,
        confidence=confidence,
        tags=tags,
        root=root,
        note=note,
        expires=expires.date() if expires else None,
    )


@main.command()
@click.argument("claim")
@click.option("--who", required=True, help="who decided: 'founder', 'the team'")
@click.option("--origin", required=True, help="where it was decided: 'planning meeting 2026-11-04'")
@click.option("--date", "when", type=click.DateTime(formats=["%Y-%m-%d"]), default=None)
@click.option("--why", default=None, help="the reason, so personas can weigh it")
def decide(claim: str, who: str, origin: str, when, why: str | None) -> None:
    """Record a settled decision in world.md and shared memory. Personas stop re-arguing it and raise doubts as questions."""
    from .service import find_decision

    ws = _ws()
    existing = find_decision(ws, claim)
    if existing is not None:
        click.echo(f"already recorded as {existing.id} on {existing.date.isoformat()}; nothing written. To replace it, `rncrn8 retire shared {existing.id} --why ...` first")
        return
    e = decide_service(ws, claim, who=who, origin=origin, when=when.date() if when else None, why=why)
    click.echo(_fmt_entry(e, verbose=True))
    click.echo(f"written under ## Decisions in {ws.world_path}")


@main.command()
@click.argument("persona_name", metavar="PERSONA")
@click.option("--query", "-q", default=None, help="substring of claim, note or origin")
@click.option("--tag", "tags", multiple=True)
@click.option("--source", type=click.Choice([s.value for s in SourceType]), default=None)
@click.option("--json", "as_json", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
def recall(persona_name, query, tags, source, as_json, verbose) -> None:
    """Print what PERSONA remembers (its own memory plus the shared store)."""
    ws = _ws()
    _require_persona(ws, persona_name)
    entries = MemoryStore(ws).search(persona_name, query=query, tags=list(tags) or None, source=SourceType(source) if source else None)
    if as_json:
        click.echo(json.dumps([e.model_dump(mode="json") for e in entries], indent=2))
        return
    if not entries:
        click.echo("nothing remembered")
        return
    for e in entries:
        click.echo(_fmt_entry(e, verbose=verbose))
        click.echo()


# --- judge / outcome ------------------------------------------------------------------


def _ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


@main.command()
@click.argument("persona_name", metavar="PERSONA")
@click.option("--claim", required=True, help="the claim being judged")
@click.option("--supports", default=None, help="comma-separated memory ids that support the claim")
@click.option("--opposes", default=None, help="comma-separated memory ids that oppose the claim")
@click.option("--no-record", is_flag=True, help="do not store the decision for later outcomes")
@click.option("--json", "as_json", is_flag=True)
def judge(persona_name, claim, supports, opposes, no_record, as_json) -> None:
    """Weigh memories for and against a claim and decide whether PERSONA raises it."""
    ws = _ws()
    try:
        j = judge_service(ws, persona_name, claim, _ids(supports), _ids(opposes), record=not no_record)
    except (KeyError, FileNotFoundError) as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        payload = {
            "persona": j.persona,
            "claim": j.claim,
            "belief": j.evidence.belief,
            "log_odds": j.evidence.log_odds,
            "threshold": j.decision.threshold,
            "raise_finding": j.decision.raise_finding,
            "severity": j.decision.severity,
            "conflict": j.evidence.conflict,
            "decision_id": j.record.id if j.record else None,
            "contributions": [c.__dict__ for c in j.evidence.ranked()],
        }
        click.echo(json.dumps(payload, indent=2))
        return

    d = j.decision
    verdict = "RAISE" if d.raise_finding else "hold"
    click.echo(f"{j.persona} on: {j.claim}")
    click.echo(f"  belief {j.evidence.belief:.2f}  (prior {j.evidence.prior:.2f}, log-odds {j.evidence.log_odds:+.2f})")
    click.echo(f"  decision {verdict}: threshold {d.threshold:.2f}, cost of silence {d.expected_cost_of_silence:.2f} vs speaking {d.expected_cost_of_speaking:.2f}")
    if j.evidence.conflict:
        click.echo(f"  CONFLICT: evidence for {j.evidence.evidence_for:.2f} vs against {j.evidence.evidence_against:.2f}")
    by_ref = {e.id: e for e, _ in j.witnesses}
    for c in j.evidence.ranked():
        e = by_ref[c.ref]
        sign = "+" if c.supports else "-"
        factors = []
        if c.decay < 0.995:
            factors.append(f"decay {c.decay:.2f}")
        if c.dependence < 0.995:
            factors.append(f"dependence {c.dependence:.2f}")
        if c.stale_factor < 0.995:
            factors.append("stale")
        if c.weight == 0.0:
            factors.append("uninformative (reliability <= 0.5)")
        extra = f"  [{', '.join(factors)}]" if factors else ""
        click.echo(f"  {sign} {c.contribution:+.2f}  {e.id} [{e.source.value}, {e.confidence:.2f}] {e.claim[:70]}{extra}")
        click.echo(f"           from {e.origin}" + (f", {e.who}" if e.who else ""))
    if j.record:
        click.echo(f"  recorded as {j.record.id}  (close it later with `rncrn8 outcome {j.record.id} --confirmed|--refuted`)")


@main.command()
@click.argument("decision_id", metavar="ID")
@click.option("--confirmed/--refuted", "claim_true", default=None, required=True, help="did the claim turn out true?")
@click.option("--note", default=None)
@click.option("--source", type=click.Choice(["user", "check", "loop"]), default=None,
              help="who stands behind it: you ('user', the default at a terminal), a check you saw ('check'), or an automated "
                   "loop ('loop', kept but teaches nothing). Required when not run from a terminal")
def outcome(decision_id: str, claim_true: bool, note: str | None, source: str | None) -> None:
    """Record what actually happened. ID is a decision id (d-...) or a review finding (r-...:critic:F1)."""
    import sys

    from .outcome_source import FROM_CALLER

    if source is None:
        if not sys.stdin.isatty():
            # a script or a hook: it must say who stands behind the outcome, or it would be recorded as a person
            raise click.UsageError("pass --source user|check|loop when outcome is not run from a terminal")
        source = "user"
    ws = _ws()
    try:
        res = record_outcome(ws, decision_id, claim_true, note, source=FROM_CALLER[source])
    except (KeyError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    word = "confirmed" if claim_true else "refuted"
    click.echo(f"{res.record.id} {word}: '{res.record.claim}' (belief was {res.record.belief:.2f})")
    if res.replaced:
        click.echo(f"  replaced an unconfirmed outcome ({res.replaced})")
    if source == "loop":
        click.echo(f"  recorded from a loop: linked from {len(res.linked or [])} memories it cited, but no trust or weight moved")
    for e in res.updated:
        click.echo(f"  {e.id} -> conf {e.confidence:.2f} (n={e.reliability.strength:.1f})  {e.origin}")
    if res.origin_updates:
        click.echo("  origins learned:")
        for origin, counts in res.origin_updates.items():
            click.echo(f"    {origin}: {counts.mean:.2f} (n={counts.strength:.1f})")
    if res.panel_losses and len(res.panel_weights) < 2:
        click.echo("  panel: " + ", ".join(f"{k} loss {v:.2f}" for k, v in res.panel_losses.items()) + "; weights compare personas, and only one has a record so far")
    elif res.panel_losses:
        click.echo("  panel: " + ", ".join(f"{k} loss {v:.2f} -> weight {res.panel_weights.get(k, 0):.2f}" for k, v in res.panel_losses.items()))


@main.command()
@click.option("--persona", "persona_name", default=None)
def calibration(persona_name: str | None) -> None:
    """How each persona's stated confidence compares with what happened."""
    from .engine import Scorecard

    ws = _ws()
    config = ws.load_config()
    decisions = MemoryStore(ws).load_decisions()
    points = reading_points(decisions)
    cals = fit_calibrators(decisions, config.calibration)
    names = [persona_name] if persona_name else sorted(points)
    if not names or not any(points.get(n) for n in names):
        click.echo("no closed decisions with persona readings yet; record outcomes first")
        return
    for name in names:
        pts = points.get(name, [])
        if not pts:
            click.echo(f"{name}: no outcomes yet")
            continue
        card = Scorecard.of([x for x, _ in pts], [y for _, y in pts])
        cal = cals[name]
        if cal.identity:
            state = "taken at its word (too few points)"
        else:
            blocks = " · ".join(f"{b.lo:.2f}" + (f"-{b.hi:.2f}" if b.hi > b.lo else "") + f": came true {round(b.mean * b.n)}/{b.n}, record counts {cal.block_alpha(b):.0%}" for b in cal.blocks)
            state = f"isotonic fit blended with its word block by block: {blocks}"
        # with at least one point, brier, log loss and ece are defined; only auc needs both outcomes present
        auc_text = _fmt_or_na(card.auc, 2) + ("" if math.isfinite(card.auc) else " (every outcome had the same answer)")
        # "came true": what the persona stated at that confidence, a claim or its disagreement with one, turned out so;
        # a low confidence on something that did not come true is a good reading, which `panel` counts as the right side
        click.echo(f"{name}: {card.n} {'reading' if card.n == 1 else 'readings'} with outcomes · came true {sum(1 for _, y in pts if y)}/{card.n} · brier {card.brier:.3f} · log loss {card.log_loss:.3f} · auc {auc_text} · ece {card.ece:.3f}")
        click.echo(f"  {state}")
        click.echo("  stated -> calibrated: " + "  ".join(f"{x:.1f}->{y:.2f}" for x, y in cal.curve()))


@main.command()
def panel() -> None:
    """Hedge weights per persona from the Brier loss of their readings."""
    ws = _ws()
    state = PanelState.load(ws.panel_path)
    if not state.weights:
        click.echo("no panel weights yet; they form as outcomes are recorded on reviews with readings")
        return
    width = max(12, *(len(name) for name in state.weights))
    for name, w in sorted(state.weights.items(), key=lambda kv: kv[1], reverse=True):
        mb = state.mean_brier(name)
        n = state.n.get(name, 0)
        click.echo(f"{name:{width}} weight {w:.2f} · {n} {'outcome' if n == 1 else 'outcomes'} · right side {state.right.get(name, 0)}/{n} · mean brier {mb:.3f}" if mb is not None else f"{name:{width}} weight {w:.2f}")
    if len(state.weights) == 1:
        click.echo("one persona has a record; weights compare personas, so its weight is 1.00 by definition")
    else:
        click.echo("weight follows mean brier, not right side: a confident call that turns out right raises it more than a hesitant one, "
                   "and a confident miss lowers it more; brier 0 is perfect, 0.25 is saying 50% every time")
    cfg = ws.load_config()
    click.echo(f"applied to reading witnesses: {'yes' if cfg.panel.apply_weights else 'no'} · influence {cfg.panel.influence}")


@main.command()
@click.option("--candidate", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="a config.yaml to compare against the current one")
@click.option("--holdout", type=click.FloatRange(0.05, 0.9), default=0.2, show_default=True, help="newest fraction of closed decisions held out for scoring")
@click.option("--without-learning", is_flag=True, help="also score the held-out decisions with no calibration and equal panel weights, to see whether the learning helped")
@click.option("--json", "as_json", is_flag=True)
def replay(candidate: Path | None, holdout: float, without_learning: bool, as_json: bool) -> None:
    """Re-score closed decisions under the current config, and a candidate, on held-out data."""
    from .config import EngineConfig
    from .replay import NO_LEARNING, compare_learning
    from .replay import replay as run_replay

    ws = _ws()
    cand = EngineConfig.load(candidate) if candidate else None
    report = run_replay(ws, cand, holdout, without_learning)
    if as_json:
        click.echo(json.dumps(report.as_dict(), indent=2))
        return
    for note in report.notes:
        click.echo(f"note: {note}")
    if not report.results:
        return
    click.echo(f"{'config':10} {'train':>5} {'test':>5} {'brier':>7} {'logloss':>8} {'auc':>6} {'ece':>6}")
    first = report.results[0]
    r = first.recorded
    click.echo(f"{'recorded':10} {'-':>5} {r.n:>5} {_fmt_or_na(r.brier, 3):>7} {_fmt_or_na(r.log_loss, 3):>8} {_fmt_or_na(r.auc, 2):>6} {_fmt_or_na(r.ece, 3):>6}   (beliefs as stored at decision time)")
    for res in report.results:
        c = res.replayed
        click.echo(f"{res.label:10} {res.n_train:>5} {c.n:>5} {_fmt_or_na(c.brier, 3):>7} {_fmt_or_na(c.log_loss, 3):>8} {_fmt_or_na(c.auc, 2):>6} {_fmt_or_na(c.ece, 3):>6}")
    if any(res.replayed.n == 0 for res in report.results):
        n_test = report.results[0].n_test
        click.echo(f"n/a: none of the {n_test} held-out decision(s) stored raw witness fields, so nothing could be replayed")
    elif any(math.isnan(res.replayed.auc) for res in report.results):
        click.echo("auc n/a: every held-out outcome had the same answer, so ranking cannot be scored")
    current = report.results[0].replayed
    for res in report.results[1:]:
        other = res.replayed
        comparable = all(math.isfinite(v) for v in (current.brier, current.log_loss, other.brier, other.log_loss))
        if res.label == NO_LEARNING:
            cmp = compare_learning(report.results[0], res)
            if cmp.low is None:
                click.echo(f"learning vs no learning: {cmp.verdict}")
            else:
                click.echo(f"learning vs no learning on {cmp.n} held-out decisions: {cmp.verdict} (Brier difference {cmp.difference:+.3f}, "
                           f"95% interval {cmp.low:+.3f} to {cmp.high:+.3f}; negative means learning did better). Covers calibration and "
                           "panel weights; memory and source trust already learned stay in both")
        elif not comparable:
            click.echo("candidate vs current: not comparable, metrics undefined on this split")
        else:
            better = other.brier < current.brier and other.log_loss <= current.log_loss
            click.echo("candidate is better on held-out decisions: " + ("yes (lower brier, no worse log loss)" if better else "no"))


@main.command()
@click.option("--persona", "persona_name", default=None)
@click.option("--open", "only_open", is_flag=True, help="only decisions without an outcome")
@click.option("--review", "review_id", default=None, help="only decisions from this review id")
@click.option("--json", "as_json", is_flag=True)
def decisions(persona_name: str | None, only_open: bool, review_id: str | None, as_json: bool) -> None:
    """List recorded decisions with their full claims."""
    from .outcome_source import normalise, teaches

    ws = _ws()
    recs = MemoryStore(ws).load_decisions(persona_name)
    if only_open:
        # a merged or conceded finding is not open: a merge took its outcome from the finding that stands, a concession has none to record
        recs = [r for r in recs if r.outcome is None and not r.withdrawn]
    if review_id:
        recs = [r for r in recs if r.review_id == review_id]
    if as_json:
        click.echo(json.dumps([r.model_dump(mode="json") for r in recs], indent=2))
        return
    if not recs:
        click.echo("no decisions")
        return
    for r in recs:
        status = "open" if r.outcome is None else ("confirmed" if r.outcome else "refuted")
        if r.outcome is not None:
            # who recorded it, so an outcome nobody here gave can be spotted
            status += f", source {normalise(r.outcome_source)}" + ("" if teaches(r.outcome_source) else ", teaches nothing")
            if r.outcome_at:
                status += f", recorded {r.outcome_at.strftime('%Y-%m-%d %H:%M UTC')}"
        verdict = "RAISED" if r.raise_finding else "held"
        if r.withdrawn:
            verdict = r.withdrawn_how or "withdrawn in the debate"
            if r.outcome is None:
                status = f"closed with {r.withdrawn_into}" if r.withdrawn_into else "not closable"
        origin = f"review {r.review_id}" if r.review_id else "judge"
        where = f" {r.where}" if r.where else ""
        kind = f" {r.kind}" if r.kind else ""
        click.echo(f"{r.id}  {r.persona} · {verdict} · belief {r.belief:.2f} · {status} · {origin}{where}{kind}")
        click.echo(f"    {r.claim}")
        if r.outcome_note:
            click.echo(f"    note: {r.outcome_note}")
        click.echo()


# --- review ---------------------------------------------------------------------------


def _build_target(target: Path | None, kind: str | None, title: str | None, diff: str | None, text: str | None, window: float | None, repo: Path, max_chars: int | None = None, paths: tuple[str, ...] = ()):
    """One packet from whichever of TARGET, --diff or --text was given."""
    given = [x for x in (target, diff, text) if x]
    if len(given) != 1:
        raise click.ClickException("give exactly one of: a TARGET path, --diff <spec>, or --text '...'")
    if paths and not diff:
        raise click.ClickException("--path works with --diff only; to review one file, give its path as the target")
    try:
        if diff:
            packet = git_diff_packet(repo, diff, title, paths=paths)
        elif text:
            if not text.strip():
                raise click.ClickException("--text is empty")
            packet = text_packet(text, title or "pitch", kind or "idea")
        else:
            packet = build_packet(target, kind=kind, title=title, window=window)
    except (AdapterError, FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if max_chars is not None and packet.reviewed_chars > max_chars:
        sizes: dict[str, int] = {}
        for s in packet.sections:
            if s.context:
                continue
            path = s.ref.rsplit(":", 1)[0] if re.fullmatch(r".+:\d+(-\d+)?", s.ref) else s.ref
            sizes[path] = sizes.get(path, 0) + len(s.text)
        biggest = ", ".join(f"{p} ({n:,})" for p, n in sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)[:5])
        hint = "Review part of it with --path, for example --path src" if diff else "Review a smaller file"
        raise click.ClickException(
            f"target is {packet.reviewed_chars:,} characters, above review.max_packet_chars ({max_chars:,}); every persona call would carry it. "
            f"The largest parts: {biggest}. {hint}, or pass --force to proceed anyway"
        )
    return packet


_TARGET_OPTIONS = [
    click.argument("target", type=click.Path(exists=True, path_type=Path), required=False),
    click.option("--kind", type=click.Choice(KINDS), default=None, help="target kind; default: what the adapter decides"),
    click.option("--title", default=None),
    click.option("--diff", default=None, metavar="SPEC", help="review a git diff: A..B, staged, worktree, or a revision to compare the working tree against (HEAD = uncommitted changes)"),
    click.option("--text", default=None, help="review this text directly, as an idea unless --kind says otherwise"),
    click.option("--window", type=float, default=None, help="video: seconds per timeline window (default 5)"),
    click.option("--repo", type=click.Path(file_okay=False, exists=True, path_type=Path), default=Path("."), help="git repository for --diff (default: here)"),
    click.option("--path", "paths", multiple=True, metavar="PATH", help="with --diff: review only these files or folders; repeatable, e.g. --path src --path tests"),
]


def _target_options(fn):
    for opt in reversed(_TARGET_OPTIONS):
        fn = opt(fn)
    return fn


@main.command()
@_target_options
@click.option("--json", "as_json", is_flag=True, help="print the packet as JSON instead of the rendered text a persona sees")
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path), default=None, help="write the packet JSON here")
def packet(target, kind, title, diff, text, window, repo, paths, as_json, out) -> None:
    """Build the review packet for a target and show it, without reviewing. Useful to check an adapter."""
    pk = _build_target(target, kind, title, diff, text, window, repo, paths=paths)
    if out:
        out.write_text(pk.model_dump_json(indent=2), encoding="utf-8")
        click.echo(f"wrote {out} ({len(pk.sections)} sections, {pk.char_count} chars, kind {pk.kind})")
        return
    click.echo(pk.model_dump_json(indent=2) if as_json else pk.render(), nl=as_json)


@main.command()
@_target_options
@click.option("--panel", default=None, help="comma-separated persona names; default: every persona that applies to the kind")
@click.option("--adapter", type=click.Choice(ADAPTERS), default=None, help="model adapter; default from config.yaml")
@click.option("--model", default=None, help="model id override for every persona")
@click.option("--dry-run", is_flag=True, help="build the prompts, call no model, record nothing")
@click.option("--show-prompts", is_flag=True, help="print each persona's system and user prompt")
@click.option("--max-findings", type=int, default=None)
@click.option("--force", is_flag=True, help="review a target larger than review.max_packet_chars anyway")
@click.option("--debate/--no-debate", default=None, help="second round where personas answer each other (default from config)")
@click.option("--summaries/--no-summaries", default=None, help="personas rewrite their summary.md afterwards (default from config)")
@click.option("--json", "as_json", is_flag=True)
def review(target, kind, title, diff, text, window, repo, paths, panel, adapter, model, dry_run, show_prompts, max_findings, force, debate, summaries, as_json) -> None:
    """Review a target: each persona in its own call with its own memory, then a debate round.

    TARGET is a text or markdown file, a .patch, an .srt/.vtt, a video folder (transcript + frames),
    or a packet JSON. Or use --diff for a git diff, or --text for an inline pitch.
    """
    ws = _ws()
    config = ws.load_config()
    packet = _build_target(target, kind, title, diff, text, window, repo, max_chars=None if (force or dry_run) else config.review.max_packet_chars, paths=paths)
    target = target or (f"diff:{diff}" if diff else "text")
    try:
        model_adapter = make_adapter("dry-run" if dry_run else adapter, config.models, model=model)
    except (KeyError, ImportError) as exc:
        raise click.ClickException(str(exc)) from exc
    names = _ids(panel) or None
    try:
        out = run_review(
            ws, packet, model_adapter, panel=names, dry_run=dry_run, debate=debate, summaries=summaries, max_findings=max_findings, model=model, target=target
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if show_prompts or (dry_run and not as_json):
        shown_systems: set[str] = set()
        for key, (system, user) in out.prompts.items():
            if system not in shown_systems:
                click.echo(f"===== {key}: SYSTEM PROMPT =====")
                click.echo(system)
                shown_systems.add(system)
            click.echo(f"===== {key}: USER PROMPT =====")
            click.echo(user)
            click.echo()
    if as_json:
        click.echo(out.review.model_dump_json(indent=2))
        if out.review.all_failed():
            raise click.exceptions.Exit(1)
        return
    click.echo(render_report(out.review), nl=False)
    if out.review_dir:
        click.echo(f"\nsaved to {out.review_dir}")
    if out.review.all_failed():
        raise click.exceptions.Exit(1)


@main.command()
@click.argument("review_id")
@click.option("--to", default="HEAD", show_default=True, help="revision to compare the reviewed commit against")
@click.option("--dry-run", is_flag=True, help="show which findings were touched, record nothing")
def followup(review_id: str, to: str, dry_run: bool) -> None:
    """Close code findings whose cited lines changed since the reviewed commit (implicit outcomes)."""
    from .followup import followup as run_followup

    ws = _ws()
    try:
        res = run_followup(ws, review_id, to=to, dry_run=dry_run)
    except (FileNotFoundError, ValueError, AdapterError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{res.review_id}: {res.base[:7]} -> {res.top[:7]}")
    for n in res.notes:
        click.echo(f"note: {n}")
    for it in res.items:
        if it.touched:
            state = "touched -> confirmed" if it.recorded else ("touched (dry run)" if dry_run else f"touched, not recorded: {it.note}")
        else:
            state = "untouched, stays open" if not it.note else it.note
        click.echo(f"  {it.decision_id}  {it.persona} {it.where}: {state}")
        click.echo(f"      {it.claim}")


# --- memory sources -------------------------------------------------------------------------


def _ingest_summary(res) -> None:
    for e in res.recorded:
        click.echo(_fmt_entry(e))
        click.echo()
    bits = [f"{len(res.recorded)} recorded"]
    if res.skipped_duplicates:
        bits.append(f"{res.skipped_duplicates} duplicates skipped")
    if res.refreshed:
        bits.append(f"{res.refreshed} refreshed")
    if res.marked_stale:
        bits.append(f"{res.marked_stale} marked stale (the source no longer says so; tagged superseded)")
    if res.notes:
        bits.append(f"notes: {res.notes}")
    if res.error:
        bits.append(f"model: {res.error}")
    click.echo(f"{res.source} -> {res.persona}: " + " · ".join(bits))
    if "facts" in res.extra:
        click.echo("dry run, would record:")
        for c in res.extra["facts"]:
            click.echo(f"  - {c}")


@main.group()
def ingest() -> None:
    """Fill memory from a source, with provenance: project, history, notes."""


@ingest.command("project")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path), required=False)
@click.option("--persona", "persona_name", default="shared", show_default=True)
@click.option("--adapter", type=click.Choice(ADAPTERS), default=None)
@click.option("--max-facts", type=int, default=None)
@click.option("--dry-run", is_flag=True, help="show what would be recorded")
def ingest_project_cmd(path, persona_name, adapter, max_facts, dry_run) -> None:
    """Read the project's README, docs and manifests into project memories."""
    from .sources import ingest_project

    ws = _ws()
    cfg = ws.load_config()
    model_adapter = make_adapter("dry-run" if dry_run else adapter, cfg.models)
    try:
        res = ingest_project(ws, model_adapter, path, persona=persona_name, max_facts=max_facts or cfg.sources.max_facts, dry_run=dry_run)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"files read: {', '.join(res.extra.get('files', [])) or 'none'}")
    _ingest_summary(res)


@ingest.command("history")
@click.option("--repo", type=click.Path(exists=True, file_okay=False, path_type=Path), default=None)
@click.option("--since", default="180 days ago", show_default=True, help="git --since value")
@click.option("--max-commits", type=int, default=None)
@click.option("--persona", "persona_name", default="shared", show_default=True)
@click.option("--adapter", type=click.Choice(ADAPTERS), default=None)
@click.option("--max-facts", type=int, default=None)
@click.option("--dry-run", is_flag=True)
def ingest_history_cmd(repo, since, max_commits, persona_name, adapter, max_facts, dry_run) -> None:
    """Turn git history into history memories: reverts, hotspots, fixes, and decisions found in messages."""
    from .sources import ingest_history

    ws = _ws()
    cfg = ws.load_config()
    model_adapter = make_adapter("dry-run" if dry_run else adapter, cfg.models)
    try:
        res = ingest_history(ws, model_adapter, repo, since=since, max_commits=max_commits or cfg.sources.history_commits, persona=persona_name, max_facts=max_facts or cfg.sources.max_facts, dry_run=dry_run)
    except (ValueError, FileNotFoundError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"commits read: {res.extra.get('commits', 0)}")
    _ingest_summary(res)


@ingest.command("notes")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--who", required=True, help="who said it: 'team lead', 'interviewee A'")
@click.option("--origin", required=True, help="where: 'customer call 2026-11-03', 'slack thread'")
@click.option("--date", "when", type=click.DateTime(formats=["%Y-%m-%d"]), default=None)
@click.option("--persona", "persona_name", default="shared", show_default=True)
@click.option("--adapter", type=click.Choice(ADAPTERS), default=None)
@click.option("--max-facts", type=int, default=None)
@click.option("--dry-run", is_flag=True)
def ingest_notes_cmd(path, who, origin, when, persona_name, adapter, max_facts, dry_run) -> None:
    """Import call notes or a transcript as human memories attributed to the speaker."""
    from .sources import ingest_notes

    ws = _ws()
    cfg = ws.load_config()
    model_adapter = make_adapter("dry-run" if dry_run else adapter, cfg.models)
    try:
        res = ingest_notes(ws, model_adapter, path, who=who, origin=origin, persona=persona_name, when=when.date() if when else None, max_facts=max_facts or cfg.sources.max_facts, dry_run=dry_run)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    _ingest_summary(res)


@main.command()
@click.option("--persona", "persona_name", default="shared", show_default=True)
@click.option("--who", default="the user", show_default=True)
def ask(persona_name: str, who: str) -> None:
    """Five questions, two minutes: what a person knows that no file says. Empty answers are skipped."""
    from .sources import QUESTIONS, record_answers

    ws = _ws()
    answers: dict[str, str] = {}
    for key, question, _ in QUESTIONS:
        answers[key] = click.prompt(question, default="", show_default=False)
    entries = record_answers(ws, answers, who=who, persona=persona_name)
    for e in entries:
        click.echo(_fmt_entry(e))
    click.echo(f"{len(entries)} answers recorded as human memories for {persona_name}")


@main.command()
@click.argument("persona_name", metavar="PERSONA")
@click.option("--question", "-q", required=True)
@click.option("--budget", type=int, default=None, help="web searches allowed (default from config)")
@click.option("--max-facts", type=int, default=12, show_default=True)
@click.option("--shared", is_flag=True, help="record into the shared store instead of the persona's own memory")
@click.option("--adapter", type=click.Choice(ADAPTERS), default=None)
@click.option("--dry-run", is_flag=True)
def research(persona_name, question, budget, max_facts, shared, adapter, dry_run) -> None:
    """Send one persona to the web with a budget; it remembers what it found, with URLs and dates."""
    from .sources import research as run_research

    ws = _ws()
    cfg = ws.load_config()
    model_adapter = make_adapter("dry-run" if dry_run else adapter, cfg.models)
    try:
        res = run_research(ws, model_adapter, persona_name, question, budget=budget or cfg.sources.research_budget, max_facts=max_facts, shared=shared, dry_run=dry_run)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    for e in res.recorded:
        click.echo(_fmt_entry(e))
        click.echo()
    where = "shared" if shared else persona_name
    click.echo(f"research by {persona_name} -> {where}: {len(res.recorded)} recorded, {res.skipped_duplicates} duplicates, {len(res.sources)} search results seen")
    if res.notes:
        click.echo(f"notes: {res.notes}")
    if res.notes_entry:
        click.echo(f"notes kept as {res.notes_entry.id} in {persona_name}'s memory")
    if res.error:
        raise click.ClickException(res.error)


@main.command()
@click.option("--workspace", type=click.Path(exists=True, file_okay=False, path_type=Path), default=None, help="project directory holding .rncrn8 (default: here)")
def mcp(workspace: Path | None) -> None:
    """Run the MCP server over stdio, so Claude Code, Cursor and other clients call these personas."""
    from .mcp_server import main as mcp_main

    mcp_main(workspace)


@main.group()
def reviews() -> None:
    """List and show past reviews."""


@reviews.command("list")
def reviews_list() -> None:
    ws = _ws()
    items = list_reviews(ws)
    if not items:
        click.echo("no reviews yet")
        return
    for r in items:
        if r.debated:  # the report's sections: a merged or conceded finding is neither raised nor held
            counts = Counter(f.status for _, f in r.all_findings())
            raised, held = counts["agreed"] + counts["disputed"] + counts["unchallenged"], counts["held"]
            extra = "".join(f"{k}={counts[k]} " for k in ("merged", "dropped") if counts[k])
        else:
            raised, held, extra = len(r.raised()), len(r.held()), ""
        who = ",".join(p.persona for p in r.personas)
        flags = ("debated " if r.debated else "") + ("dry-run " if r.dry_run else "")
        click.echo(f"{r.id}  {r.created_at.strftime('%Y-%m-%d %H:%M')}  {r.kind:8} raised={raised:<3} held={held:<3} {extra}{flags:9}[{who}] {r.title[:50]}")


@reviews.command("show")
@click.argument("review_id")
@click.option("--json", "as_json", is_flag=True)
def reviews_show(review_id: str, as_json: bool) -> None:
    ws = _ws()
    try:
        r = load_review(ws, review_id)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(r.model_dump_json(indent=2) if as_json else render_report(r), nl=as_json)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
