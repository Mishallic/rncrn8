from __future__ import annotations

from pathlib import Path

import yaml

from ..memory.store import Workspace
from .models import Persona
from .templates import PANELS, TEMPLATES

class InvalidPersonaFile(ValueError):
    """A persona.yaml that cannot be read; the message names the persona and the field."""


SUMMARY_TEMPLATE = """# {display_name}: summary

Maintained by the persona itself after each session. You may edit it.

## What I tend to get right

## What I was told to stop raising

## What I am tracking
"""


def persona_path(ws: Workspace, name: str) -> Path:
    return ws.persona_dir(name) / "persona.yaml"


def save_persona(ws: Workspace, persona: Persona) -> Path:
    d = ws.persona_dir(persona.name)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "persona.yaml"
    path.write_text(yaml.safe_dump(persona.model_dump(mode="json"), sort_keys=False, allow_unicode=True), encoding="utf-8")
    summary = d / "summary.md"
    if not summary.exists():
        summary.write_text(SUMMARY_TEMPLATE.format(display_name=persona.display_name), encoding="utf-8")
    (d / "memory.jsonl").touch()
    return path


def load_persona(ws: Workspace, name: str) -> Persona:
    path = persona_path(ws, name)
    if not path.exists():
        raise FileNotFoundError(f"no persona named '{name}' in {ws.personas_dir}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        return Persona.model_validate(data)
    except ValueError as exc:
        detail = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]) if hasattr(exc, "errors") else str(exc)
        raise InvalidPersonaFile(f"persona '{name}' ({path}) is invalid: {detail}") from exc


def list_personas(ws: Workspace) -> list[Persona]:
    if not ws.personas_dir.exists():
        return []
    out = []
    for d in sorted(ws.personas_dir.iterdir()):
        if (d / "persona.yaml").exists():
            out.append(load_persona(ws, d.name))
    return out


def scaffold_persona(ws: Workspace, name: str, template: str | None = None, display_name: str | None = None) -> Persona:
    if persona_path(ws, name).exists():
        raise FileExistsError(f"persona '{name}' already exists")
    if template:
        if template not in TEMPLATES:
            raise KeyError(f"unknown template '{template}'; available: {', '.join(sorted(TEMPLATES))}")
        base = TEMPLATES[template]
        persona = base.model_copy(update={"name": name, "display_name": display_name or base.display_name})
    else:
        persona = Persona(
            name=name,
            display_name=display_name or name.replace("-", " ").replace("_", " ").title(),
            character="(describe who this persona is and how it reads things)",
            rubric=["(first thing it checks)", "(second thing it checks)"],
        )
    save_persona(ws, persona)
    return persona


def scaffold_panel(ws: Workspace, panel: str) -> tuple[list[Persona], list[str]]:
    """Create every persona of a recommended panel that is not here yet, from the template of the same
    name. Returns (created, already present); personas already in the workspace are left untouched."""
    if panel not in PANELS:
        raise KeyError(f"unknown panel '{panel}'; available: {', '.join(PANELS)}")
    created: list[Persona] = []
    existing: list[str] = []
    for name in PANELS[panel]:
        if persona_path(ws, name).exists():
            existing.append(name)
        else:
            created.append(scaffold_persona(ws, name, template=name))
    return created, existing
