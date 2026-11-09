"""Checks behind `rncrn8 doctor`: the install, the shell, the workspace. One line each, no tracebacks.

Levels: ok, warn (works, but something is worth knowing), fail (a review will not run until it is
fixed), skip (not checked this time). Every detail line says what to do, not only what is wrong.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import __version__, connect
from .config import EngineConfig, ModelSettings
from .memory import MemoryStore, Workspace
from .memory.legacy import tracked
from .memory.store import SHARED_PERSONA, WORKSPACE_DIR
from .models import API_KEY_PREFIXES, ModelError, ModelRequest, default_model, make_adapter
from .persona.loader import load_persona

_PLACEHOLDER = "- (date) decision, and why"


@dataclass(frozen=True)
class Check:
    level: str
    name: str
    detail: str


def _short(exc: BaseException, limit: int = 300) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}"[:limit] if text else type(exc).__name__


def run_checks(ws: Workspace | None, live: bool = False, adapter_name: str | None = None, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> list[Check]:
    env = os.environ if env is None else env
    out: list[Check] = []

    v = sys.version_info
    ok_py = v >= (3, 11)
    out.append(Check("ok" if ok_py else "fail", "python", f"{v.major}.{v.minor}.{v.micro} at {sys.executable}" + ("" if ok_py else "; 3.11 or newer is needed")))
    out.append(Check("ok", "rncrn8", __version__))
    try:
        import anthropic

        out.append(Check("ok", "anthropic sdk", str(getattr(anthropic, "__version__", "installed"))))
    except ImportError as exc:
        out.append(Check("fail", "anthropic sdk", f"not importable ({exc}); reinstall the package"))
    try:
        import mcp

        out.append(Check("ok", "mcp", f"{getattr(mcp, '__version__', 'installed')}; `rncrn8 mcp` serves Claude Code, Cursor and other clients"))
    except ImportError:
        out.append(Check("warn", "mcp", "not importable; `rncrn8 mcp` will not run, everything else will"))
    git = shutil.which("git")
    if git:
        try:
            ver = subprocess.run([git, "--version"], capture_output=True, text=True, timeout=10, check=False).stdout.strip()
            out.append(Check("ok", "git", ver or git))
        except (OSError, subprocess.SubprocessError) as exc:
            out.append(Check("warn", "git", f"found but not working ({_short(exc)})"))
    else:
        out.append(Check("warn", "git", "not found; --diff reviews, followup and ingest history need it"))

    claude = connect.find_claude()
    if claude is None:
        out.append(Check("skip", "assistant", "Claude Code not found; `rncrn8 init` prints the entry other assistants take"))
    elif connect.registered(claude):
        out.append(Check("ok", "assistant", "Claude Code has rncrn8; ask it for a review, or type /rncrn8:review"))
    else:
        out.append(Check("warn", "assistant", "Claude Code is installed but rncrn8 is not connected to it; `rncrn8 init` connects it"))

    cfg: EngineConfig | None = None
    if ws is None:
        out.append(Check("warn", "workspace", f"none in {cwd or Path.cwd()} or above; `rncrn8 init` in the project folder creates one and connects your assistant"))
    elif ws.old_name:
        why = (f"git tracks it; `git mv {ws.root.name} {WORKSPACE_DIR}` in {ws.project_dir}, then a commit, renames it"
               if tracked(ws.project_dir, ws.root.name) else "renaming it failed; close whatever has a file in it open and run any rncrn8 command again")
        out.append(Check("warn", "workspace", f"{ws.root} still has the name from before the tool was renamed rncrn8, because {why}"))
    else:
        out.append(Check("ok", "workspace", str(ws.root)))
    if ws is not None:
        try:
            cfg = ws.load_config()
            model, came_from = default_model(cfg.models)
            shown = model if came_from == "config.yaml" else f"{model} (from {came_from}; config.yaml says {cfg.models.model})"
            out.append(Check("ok", "config", f"adapter {cfg.models.adapter}, model {shown}, effort {cfg.models.effort}"))
        except Exception as exc:  # noqa: BLE001 - a broken YAML or a bad value: name it, keep checking the rest
            out.append(Check("fail", "config", f"{ws.config_path} cannot be read: {_short(exc)}"))
        out += _workspace_checks(ws)

    adapter_kind = (adapter_name or (cfg.models.adapter if cfg else "claude")).lower()
    own = (env.get("RNCRN8_API_KEY", "") or "").strip()
    key = env.get("ANTHROPIC_API_KEY", "") or ""
    token = env.get("ANTHROPIC_AUTH_TOKEN", "") or ""
    if own or key or token:
        shown = own or key
        prefix = f"{shown[:12]}…" if shown else "auth token"
        odd = bool(shown) and not shown.startswith(API_KEY_PREFIXES)  # only a warning: `doctor --live` is the real test of a key
        hint = f" (Anthropic API keys start with {' or '.join(API_KEY_PREFIXES)}; a 401 on the first review means the wrong kind of key)" if odd else ""
        where = " in RNCRN8_API_KEY, which only rncrn8 reads" if own else ""
        shared = ("; Claude Code reads ANTHROPIC_API_KEY too and, once you approve it, bills its own use to it instead of a Claude "
                  "subscription: put the key in RNCRN8_API_KEY instead to keep the two apart") if key and not own else ""
        out.append(Check("warn" if odd else "ok", "api key", f"present, {prefix}{where}{hint}{shared}"))
        from .models.claude import WORKSPACE_VARS

        found = next(((str(env.get(n) or "").strip(), n) for n in WORKSPACE_VARS if str(env.get(n) or "").strip()), None)
        if found:
            out.append(Check("ok", "api workspace", f"{found[0]} from {found[1]}, sent with every call as anthropic-workspace-id"))
    elif _credentials_found():
        out.append(Check("ok", "api key", "none in this shell; the Anthropic SDK found an `ant auth login` profile or federation settings"))
    else:
        level = "warn" if adapter_kind in ("dry-run", "dryrun", "none") else "fail"
        out.append(Check(level, "api key", "no Anthropic key in this shell: " + 'setx RNCRN8_API_KEY "sk-ant-api03-..." in PowerShell, then restart VS Code or the terminal Claude Code runs in; export RNCRN8_API_KEY=sk-ant-api03-... in bash. Only rncrn8 reads that variable, so Claude Code keeps using your Claude login' + "; ANTHROPIC_API_KEY or `ant auth login` also work"))

    if live:
        out.append(_live_check(cfg.models if cfg else ModelSettings(), adapter_name))
    else:
        out.append(Check("skip", "model", "not called; `rncrn8 doctor --live` makes one tiny call to check the key and the model"))
    return out


def _workspace_checks(ws: Workspace) -> list[Check]:
    out: list[Check] = []
    world = ws.read_world()
    section = world.split("## Decisions", 1)[1].split("\n## ", 1)[0] if "## Decisions" in world else ""
    decisions = [l for l in section.splitlines() if l.startswith("- ") and l.strip() != _PLACEHOLDER]
    if decisions:
        out.append(Check("ok", "world", f"{len(decisions)} decision(s) recorded in world.md"))
    else:
        out.append(Check("warn", "world", "no decisions recorded; `rncrn8 decide \"...\" --who you --origin where` makes a settled choice visible to every persona"))

    names: list[str] = []
    broken: list[str] = []
    if ws.personas_dir.exists():
        for d in sorted(ws.personas_dir.iterdir()):
            if not (d / "persona.yaml").exists():
                continue
            try:
                names.append(load_persona(ws, d.name).name)
            except Exception as exc:  # noqa: BLE001 - the point is to name every unreadable file
                broken.append(f"{d.name} ({_short(exc, 120)})")
    if broken:
        out.append(Check("fail", "personas", f"unreadable: {'; '.join(broken)}" + (f"; readable: {', '.join(names)}" if names else "")))
    elif names:
        out.append(Check("ok", "personas", f"{', '.join(names)} ({len(names)})"))
    else:
        out.append(Check("warn", "personas", "none; `rncrn8 persona panel code|pitch|document|video|all` creates a recommended set"))

    store = MemoryStore(ws)
    total = stale = 0
    for name in [*names, SHARED_PERSONA]:
        try:
            entries = store.load(name)
        except Exception:  # noqa: BLE001 - a store that cannot be read is reported with its persona, not fatal here
            continue
        total += len(entries)
        stale += sum(1 for e in entries if e.is_stale())
    if total:
        out.append(Check("ok", "memory", f"{total} entries" + (f", {stale} stale (re-check or `rncrn8 retire`)" if stale else "")))
    else:
        out.append(Check("warn", "memory", "empty; `rncrn8 ingest project` reads README, docs and manifests into shared memory, `ingest history` reads git log"))

    recs = [r for r in store.load_decisions() if not r.withdrawn]  # merged or conceded: no outcome of its own to record
    closed = [r for r in recs if r.outcome is not None]
    from .outcome_source import teaches

    explicit = [r for r in closed if teaches(r.outcome_source)]
    if not recs:
        out.append(Check("skip", "outcomes", "no decisions yet; the first review creates them"))
    elif not closed:
        out.append(Check("warn", "outcomes", f"{len(recs)} {'decision' if len(recs) == 1 else 'decisions'}, none with an outcome; `rncrn8 outcome d-... --confirmed|--refuted` is what makes personas learn"))
    else:
        out.append(Check("ok", "outcomes", f"{len(closed)} of {len(recs)} {'decision' if len(recs) == 1 else 'decisions'} closed, {len(explicit)} by a person; calibration applies from 3 of those per persona"))
    return out


def _credentials_found() -> bool:
    from .models.claude import credentials_found

    return credentials_found()


def _live_check(settings: ModelSettings, adapter_name: str | None) -> Check:
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    try:
        adapter = make_adapter(adapter_name, settings)
        request = ModelRequest(system="You answer with JSON only.", user='Reply with {"ok": true}.', schema=schema, max_tokens=64, model=default_model(settings)[0], effort=settings.effort, label="doctor")
        started = time.monotonic()
        resp = adapter.complete(request)
        took = time.monotonic() - started
    except ModelError as exc:
        return Check("fail", "model", str(exc))
    except Exception as exc:  # noqa: BLE001 - whatever failed, the user needs the one line, not the stack
        return Check("fail", "model", _short(exc))
    answered = isinstance(resp.data, dict) and resp.data.get("ok") is True
    if adapter.name != "claude":
        return Check("ok", "model", f"{adapter.name} adapter answered; no model was called")
    detail = f"{resp.model or default_model(settings)[0]} answered in {took:.1f}s"
    return Check("ok" if answered else "warn", "model", detail if answered else f"{detail}, but not as asked: {resp.text[:80]!r}")
