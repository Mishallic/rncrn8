"""File-backed storage. Plain files in a git repo so memory travels with the project.

Layout of a workspace:
  .rncrn8/
    config.yaml          tunables (EngineConfig)
    world.md             shared project facts every persona reads
    reliability.json     learned per-origin reliabilities
    decisions.jsonl      every judgement, with its witnesses, for outcomes and replay
    personas/<name>/
      persona.yaml       definition and trust profile
      memory.jsonl       append-only log of MemoryEntry snapshots (latest id wins)
      summary.md         the persona's self-maintained summary

Logs are append-only: an update appends a new snapshot of the entry. Loading
collapses by id and keeps the last one, so history is never lost and files
merge cleanly in git.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from ..config import EngineConfig, SourceType
from ..engine.beta import BetaCounts
from .legacy import LEGACY_WORKSPACE_DIRS, adopt
from .lock import locked
from .models import MemoryEntry, new_id

WORKSPACE_DIR = ".rncrn8"
SHARED_PERSONA = "shared"

WORLD_TEMPLATE = """# Shared world

Facts every persona reads before forming an opinion. Keep it short and true.
Decisions already made belong here, so no persona re-argues them.

## Project

(what this project is, who it is for)

## Decisions

- (date) decision, and why

## Constraints

- (deadline, budget, platform, things that cannot change)
"""


class DecisionRecord(BaseModel):
    """One judgement by one persona, with everything needed to learn from its outcome."""

    id: str = Field(default_factory=lambda: new_id("d"))
    persona: str
    claim: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    prior: float
    log_odds: float
    belief: float
    threshold: float
    raise_finding: bool
    severity: float
    contributions: list[dict[str, Any]] = Field(default_factory=list)
    review_id: str | None = None
    where: str | None = None
    kind: str | None = None
    revision: int = Field(default=1, description="bumped each time a debate re-scores the decision; latest snapshot wins")
    withdrawn: bool = Field(default=False, description="the persona conceded the finding, or merged it into another reviewer's")
    withdrawn_how: str | None = Field(default=None, description="'conceded', or 'merged into <finding id>'")
    withdrawn_into: str | None = Field(default=None, description="for a merge: the decision id of the finding that stands; its outcome is the outcome of this one")
    outcome: bool | None = None
    outcome_at: datetime | None = None
    outcome_note: str | None = None
    outcome_source: str | None = Field(
        default=None,
        description="'explicit' (a person said so) or 'check' (a check a person saw): these teach. 'loop', 'backtest', "
        "'implicit:<review id>' (a persona's self-check) and 'implicit:commit:<sha>' are recorded but teach nothing. See outcome_source.py",
    )
    outcome_learning_rate: float | None = None


_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")


def check_persona_name(name: str) -> str:
    """Persona names are path components. Anything but a slug is refused before it touches the disk."""
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError(f"invalid persona name {name!r}: lowercase letters, digits, '-' or '_', starting with a letter")
    return name


def _append_jsonl(path: Path, record: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (record.model_dump_json() + "\n").encode("utf-8")
    # Under the file's lock: on POSIX an O_APPEND write is atomic, but on Windows append mode is a seek to the
    # end and then a write, so two writers can land on the same offset and one record overwrites the other.
    with locked(path), open(path, "ab", buffering=0) as f:
        f.write(data)


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """Every well-formed line. A line a crash truncated, or anything else that is not a JSON object
    with an id, is skipped; one bad line must never make the workspace unusable."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("id"), str):
                out.append(obj)
    return out


def _collapse(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Latest snapshot per id, in first-seen order."""
    by_id: dict[str, dict[str, Any]] = {}
    for r in records:
        by_id[r["id"]] = r
    return list(by_id.values())


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.renamed_from: Path | None = None
        """Set when finding this workspace renamed it from its old name, so the command line can say so."""

    # --- discovery ----------------------------------------------------------------

    @classmethod
    def at(cls, folder: Path) -> "Workspace | None":
        """The workspace directly inside `folder`. One under an old name is renamed on the way when it
        can be, and used where it is when it cannot (see memory/legacy.py)."""
        if (folder / WORKSPACE_DIR).is_dir():
            return cls(folder / WORKSPACE_DIR)
        for old_name in LEGACY_WORKSPACE_DIRS:
            old = folder / old_name
            if old.is_dir():
                root, moved = adopt(old, WORKSPACE_DIR)
                ws = cls(root)
                ws.renamed_from = old if moved else None
                return ws
        return None

    @classmethod
    def find(cls, start: Path | None = None) -> "Workspace | None":
        here = (start or Path.cwd()).resolve()
        for candidate in [here, *here.parents]:
            ws = cls.at(candidate)
            if ws is not None:
                return ws
        return None

    @classmethod
    def init(cls, project_dir: Path) -> "Workspace":
        """Create the workspace in `project_dir`, or complete the one already there, under either name."""
        ws = cls.at(Path(project_dir)) or cls(Path(project_dir) / WORKSPACE_DIR)
        ws.root.mkdir(parents=True, exist_ok=True)
        if not ws.config_path.exists():
            EngineConfig().save(ws.config_path)
        if not ws.world_path.exists():
            ws.world_path.write_text(WORLD_TEMPLATE, encoding="utf-8")
        ws.personas_dir.mkdir(exist_ok=True)
        return ws

    @property
    def project_dir(self) -> Path:
        return self.root.parent

    @property
    def old_name(self) -> bool:
        """Still under the name from before the rename: git tracks it, or renaming it failed."""
        return self.root.name in LEGACY_WORKSPACE_DIRS

    # --- paths --------------------------------------------------------------------

    @property
    def config_path(self) -> Path:
        return self.root / "config.yaml"

    @property
    def world_path(self) -> Path:
        return self.root / "world.md"

    @property
    def reliability_path(self) -> Path:
        return self.root / "reliability.json"

    @property
    def decisions_path(self) -> Path:
        return self.root / "decisions.jsonl"

    @property
    def personas_dir(self) -> Path:
        return self.root / "personas"

    @property
    def reviews_dir(self) -> Path:
        return self.root / "reviews"

    @property
    def panel_path(self) -> Path:
        return self.root / "panel.json"

    def persona_dir(self, name: str) -> Path:
        return self.personas_dir / name

    # --- config -------------------------------------------------------------------

    def load_config(self) -> EngineConfig:
        return EngineConfig.load(self.config_path)

    def read_world(self) -> str:
        return self.world_path.read_text(encoding="utf-8") if self.world_path.exists() else ""


class ReliabilityRegistry:
    """Learned reliability per origin. New entries from a known origin inherit what it earned."""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, BetaCounts] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
            self._data = {k: BetaCounts.model_validate(v) for k, v in raw.items()}

    def get(self, origin: str) -> BetaCounts | None:
        return self._data.get(origin)

    def set(self, origin: str, counts: BetaCounts) -> None:
        self._data[origin] = counts

    def nudge(self, origin: str, fallback: BetaCounts, agreed: bool, amount: float) -> BetaCounts:
        current = self._data.get(origin, fallback)
        updated = current.confirmed(amount) if agreed else current.contradicted(amount)
        self._data[origin] = updated
        return updated

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v.model_dump() for k, v in sorted(self._data.items())}
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def __len__(self) -> int:
        return len(self._data)


class MemoryStore:
    def __init__(self, workspace: Workspace):
        self.ws = workspace

    def memory_path(self, persona: str) -> Path:
        return self.ws.persona_dir(check_persona_name(persona)) / "memory.jsonl"

    def append(self, entry: MemoryEntry) -> MemoryEntry:
        _append_jsonl(self.memory_path(entry.persona), entry)
        return entry

    def load(self, persona: str, include_retired: bool = False) -> list[MemoryEntry]:
        out: list[MemoryEntry] = []
        for r in _collapse(_read_jsonl(self.memory_path(persona))):
            try:
                e = MemoryEntry.model_validate(r)
            except ValueError:
                continue  # a record this version cannot read is skipped, not fatal
            if e.retired and not include_retired:
                continue
            out.append(e)
        return out

    def load_visible(self, persona: str) -> list[MemoryEntry]:
        """A persona sees its own memory plus the shared store."""
        own = self.load(persona)
        if persona == SHARED_PERSONA:
            return own
        return own + self.load(SHARED_PERSONA)

    def get(self, persona: str, entry_id: str) -> MemoryEntry | None:
        for e in self.load_visible(persona):
            if e.id == entry_id:
                return e
        return None

    def search(
        self,
        persona: str,
        query: str | None = None,
        tags: list[str] | None = None,
        source: SourceType | None = None,
    ) -> list[MemoryEntry]:
        out = []
        q = query.lower() if query else None
        for e in self.load_visible(persona):
            if q and q not in e.claim.lower() and q not in (e.note or "").lower() and q not in e.origin.lower():
                continue
            if tags and not set(tags) <= set(e.tags):
                continue
            if source and e.source != source:
                continue
            out.append(e)
        return out

    # --- decisions ----------------------------------------------------------------

    def append_decision(self, record: DecisionRecord) -> DecisionRecord:
        _append_jsonl(self.ws.decisions_path, record)
        return record

    def load_decisions(self, persona: str | None = None) -> list[DecisionRecord]:
        records = []
        for r in _collapse(_read_jsonl(self.ws.decisions_path)):
            try:
                records.append(DecisionRecord.model_validate(r))
            except ValueError:
                continue
        if persona:
            records = [r for r in records if r.persona == persona]
        return records

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        for r in self.load_decisions():
            if r.id == decision_id:
                return r
        return None

    def decisions_citing(self, memory_id: str) -> list[DecisionRecord]:
        """Decisions whose witnesses include this memory entry."""
        return [r for r in self.load_decisions() if any(c.get("ref") == memory_id for c in r.contributions)]
