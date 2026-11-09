"""Project ingest: what the repository already says about itself."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..config import SourceType
from ..memory import Workspace
from ..memory.legacy import LEGACY_WORKSPACE_DIRS
from ..memory.store import WORKSPACE_DIR
from ..models import ModelAdapter
from .extract import EXTRACTION_RULES, Fact, IngestResult, extract_facts, record_facts, refresh_facts, source_hash

DOC_GLOBS = ("README*", "readme*", "CONTRIBUTING*", "ARCHITECTURE*", "DESIGN*", "CLAUDE.md", "AGENTS.md", "docs/*.md", "docs/**/*.md", "doc/*.md")
MANIFESTS = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", "requirements.txt", "Gemfile", "composer.json")
SKIP_DIRS = {".git", WORKSPACE_DIR, *LEGACY_WORKSPACE_DIRS, "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".tox", ".mypy_cache"}


def collect_project_text(root: Path, max_chars: int = 60000, per_file: int = 12000) -> list[tuple[str, str]]:
    seen: set[Path] = set()
    out: list[tuple[str, str]] = []
    total = 0
    for pattern in DOC_GLOBS + MANIFESTS:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or path in seen or any(part in SKIP_DIRS for part in path.parts):
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            text = text[:per_file]
            if total + len(text) > max_chars:
                break
            total += len(text)
            out.append((str(path.relative_to(root)), text))
    return out


def deterministic_project_facts(root: Path, files: list[tuple[str, str]]) -> list[Fact]:
    """Facts that need no model: the layout, the declared dependencies, the README's first line."""
    facts: list[Fact] = []
    top = sorted(p.name + ("/" if p.is_dir() else "") for p in root.iterdir() if p.name not in SKIP_DIRS and not p.name.startswith("."))
    if top:
        facts.append(Fact(claim=f"Top-level layout: {', '.join(top[:30])}", origin=str(root.name) + "/", confidence=0.9, tags=["layout"]))
    for rel, text in files:
        name = Path(rel).name
        if name.lower().startswith("readme"):
            first = next((l.lstrip("# ").strip() for l in text.splitlines() if l.strip()), "")
            if first:
                facts.append(Fact(claim=f"The README introduces the project as: {first[:200]}", origin=rel, confidence=0.8, tags=["readme"]))
        if name == "pyproject.toml":
            deps = re.findall(r'^\s*"([A-Za-z0-9_.\-\[\]]+)[>=<~!]', text, flags=re.M)
            if deps:
                facts.append(Fact(claim=f"Python dependencies declared: {', '.join(sorted(set(deps))[:20])}", origin=rel, confidence=0.9, tags=["dependencies"]))
        if name == "package.json":
            try:
                data = json.loads(text)
                deps = sorted((data.get("dependencies") or {}).keys())
                if deps:
                    facts.append(Fact(claim=f"JavaScript dependencies declared: {', '.join(deps[:20])}", origin=rel, confidence=0.9, tags=["dependencies"]))
            except json.JSONDecodeError:
                pass
    return facts


def ingest_project(ws: Workspace, adapter: ModelAdapter, root: Path | None = None, persona: str = "shared", max_facts: int = 25, dry_run: bool = False) -> IngestResult:
    root = Path(root or ws.project_dir)
    files = collect_project_text(root)
    result = IngestResult(source="project", persona=persona, extra={"files": [rel for rel, _ in files]})
    facts = deterministic_project_facts(root, files)
    if files:
        system = (
            "You read a software project's own documents and extract facts a reviewer should know before forming an opinion: "
            "what it is, who it is for, how it is built, decisions and their reasons, constraints, conventions, known problems.\n\n" + EXTRACTION_RULES
        )
        user = "PROJECT FILES\n\n" + "\n\n".join(f"===== {rel} =====\n{text}" for rel, text in files) + f"\n\nExtract at most {max_facts} facts. Use the file path as origin."
        model_facts, notes, error, _ = extract_facts(adapter, system, user, max_facts, label=f"ingest-project:{persona}")
        result.notes, result.error = notes, error
        facts += model_facts
    else:
        result.notes = "no README, docs or manifests found"
    if dry_run:
        result.extra["facts"] = [f.claim for f in facts]
        return result
    kept = facts[: max_facts + 5]
    hashes = project_hashes(files, facts)
    result.recorded, result.skipped_duplicates = record_facts(ws, persona, kept, SourceType.PROJECT, extra_tags=["ingest:project"], source_hashes=hashes)
    # the same files were read again: facts they still yield are current, facts whose file changed and that
    # they no longer yield are stale
    origins = {rel for rel, _ in files} | {str(root.name) + "/"}
    result.refreshed, result.marked_stale = refresh_facts(ws, persona, kept, SourceType.PROJECT, origins, source_hashes=hashes)
    return result


def project_hashes(files: list[tuple[str, str]], facts: list[Fact]) -> dict[str, str]:
    """Hash of the text each fact's origin was read from: the file's text, or for the layout fact the
    listing itself, since the layout has no file of its own."""
    hashes = {rel: source_hash(text) for rel, text in files}
    for f in facts:
        if "layout" in f.tags:
            hashes[f.origin] = source_hash(f.claim)
    return hashes
