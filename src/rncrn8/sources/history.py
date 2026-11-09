"""History ingest: what git remembers about the project."""

from __future__ import annotations

import re
import secrets
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..config import SourceType
from ..memory import MemoryStore, Workspace
from ..models import ModelAdapter
from .extract import EXTRACTION_RULES, Fact, IngestResult, extract_facts, record_facts


@dataclass
class Commit:
    sha: str
    author: str
    date: str
    subject: str
    body: str
    files: list[str] = field(default_factory=list)


def git_log(repo: Path, since: str | None = "180 days ago", max_commits: int = 200) -> list[Commit]:
    """Commits newest first, each with its files. The message ends at an explicit marker: a message has
    blank lines of its own, before trailers such as Co-Authored-By, and splitting at the first blank line
    counted those trailers as changed files."""
    # The separators are random per call: a commit author can write any byte, control characters included,
    # into a message, but cannot guess these, so no message can end its commit early or forge a file line.
    tok = secrets.token_hex(16)
    start, field_sep, end = f"<{tok}:c>", f"<{tok}:f>", f"<{tok}:e>"
    fmt = f"{start}%H{field_sep}%an{field_sep}%as{field_sep}%s{field_sep}%b{end}"
    args = ["git", "-C", str(repo), "-c", "core.quotePath=false", "log", f"--max-count={max_commits}", f"--format={fmt}", "--name-only", "--no-merges"]
    if since:
        args.append(f"--since={since}")
    try:
        res = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", check=False)
    except FileNotFoundError as exc:
        raise ValueError("git is not installed or not on PATH") from exc
    if res.returncode != 0:
        raise ValueError(f"git log failed: {res.stderr.strip()}")
    commits: list[Commit] = []
    for chunk in res.stdout.split(start):
        if not chunk.strip():
            continue
        message, _, file_list = chunk.partition(end)
        parts = message.split(field_sep, 4)
        if len(parts) < 5:
            continue
        sha, author, when, subject, body = parts
        files = [line.strip() for line in file_list.splitlines() if line.strip()]
        commits.append(Commit(sha=sha.strip(), author=author, date=when, subject=subject.strip(), body=body.strip(), files=files))
    return commits


def deterministic_history_facts(commits: list[Commit]) -> list[Fact]:
    facts: list[Fact] = []
    if not commits:
        return facts
    first, last = commits[-1].date, commits[0].date
    authors = Counter(c.author for c in commits)
    facts.append(Fact(claim=f"{len(commits)} commits by {len(authors)} author(s) between {first} and {last}", origin="git log", confidence=0.95, tags=["activity"]))
    reverts = [c for c in commits if c.subject.lower().startswith("revert")]
    for c in reverts[:5]:
        facts.append(Fact(claim=f"A change was reverted: {c.subject[:160]}", origin=f"git log {c.sha[:7]}", who=c.author, date=_d(c.date), confidence=0.9, tags=["revert"]))
    touched = Counter(f for c in commits for f in c.files)
    hot = [f"{path} ({n})" for path, n in touched.most_common(5) if n >= 3]
    if hot:
        facts.append(Fact(claim=f"Most changed files in this period: {', '.join(hot)}", origin="git log --name-only", confidence=0.9, tags=["hotspot"]))
    fixes = [c for c in commits if re.search(r"\b(fix|bug|regress|hotfix)\b", c.subject, flags=re.I)]
    if fixes:
        facts.append(Fact(claim=f"{len(fixes)} of {len(commits)} commits are fixes; most recent: {fixes[0].subject[:120]}", origin="git log", confidence=0.8, tags=["fixes"]))
    return facts


def _d(value: str):
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def ingest_history(ws: Workspace, adapter: ModelAdapter, repo: Path | None = None, since: str | None = "180 days ago", max_commits: int = 200, persona: str = "shared", max_facts: int = 25, dry_run: bool = False) -> IngestResult:
    repo = Path(repo or ws.project_dir)
    commits = git_log(repo, since, max_commits)
    result = IngestResult(source="history", persona=persona, extra={"commits": len(commits)})
    facts = deterministic_history_facts(commits)
    if commits:
        system = (
            "You read a project's git history and extract facts a reviewer should know: decisions and the reasons given, "
            "things that were tried and reverted, recurring problems, areas under heavy change, and conventions the messages reveal.\n\n" + EXTRACTION_RULES
        )
        lines = [f"{c.sha[:7]} {c.date} {c.author}: {c.subject}" + (f"\n    {c.body[:400]}" if c.body else "") for c in commits[:max_commits]]
        user = "GIT LOG (newest first)\n\n" + "\n".join(lines) + f"\n\nExtract at most {max_facts} facts. Use 'git log <sha>' as origin and the author as who."
        model_facts, notes, error, _ = extract_facts(adapter, system, user, max_facts, label=f"ingest-history:{persona}")
        result.notes, result.error = notes, error
        facts += model_facts
    else:
        result.notes = "no commits in range"
    if dry_run:
        result.extra["facts"] = [f.claim for f in facts]
        return result
    result.recorded, result.skipped_duplicates = record_facts(ws, persona, facts[: max_facts + 10], SourceType.HISTORY, extra_tags=["ingest:history"])
    result.marked_stale = supersede_snapshots(ws, persona, facts)
    return result


SNAPSHOT_TAGS = {"activity", "hotspot", "fixes"}
"""Facts that summarise the whole history read; each new read replaces the previous one."""


def supersede_snapshots(ws: Workspace, persona: str, facts: list[Fact]) -> int:
    """Mark older history snapshots stale and superseded when this read produced a newer one with the same tag.
    Nothing is deleted. Returns how many were marked."""
    from datetime import datetime, timedelta, timezone

    from .extract import _dedupe_key

    latest = {t: _dedupe_key(f.claim) for f in facts for t in f.tags if t in SNAPSHOT_TAGS}
    if not latest:
        return 0
    store = MemoryStore(ws)
    today = datetime.now(timezone.utc).date()
    marked = 0
    for e in store.load(persona):
        if e.source != SourceType.HISTORY or "superseded" in e.tags or e.is_stale(today):
            continue
        tags = SNAPSHOT_TAGS & set(e.tags)
        if any(t in latest and _dedupe_key(e.claim) != latest[t] for t in tags):
            store.append(e.model_copy(update={"expires_at": today - timedelta(days=1), "tags": sorted(set(e.tags) | {"superseded"})}))
            marked += 1
    return marked
