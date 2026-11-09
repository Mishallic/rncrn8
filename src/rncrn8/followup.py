"""Follow-ups from commits: close code findings whose cited lines changed after the review.

Only reviews made on a committed range can be followed up: the reviewed commit is
the base, HEAD (or --to) is the top, and the follow-up diff's old-side ranges say
which reviewed lines were touched. A touched finding is closed as confirmed at a
low learning rate and marked implicit; untouched findings stay open.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .adapters.diff import cited_ranges, git_diff_text, is_ancestor, ranges_touched, touched_ranges
from .memory import MemoryStore, Workspace
from .service import record_outcome

CLOSABLE_KINDS = ("problem", "risk")
"""A later change to the cited lines says something about a problem or a risk. It says nothing
about whether a strength or a question was right, so those are never closed by a commit."""

_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass
class FollowupItem:
    decision_id: str
    persona: str
    where: str
    claim: str
    touched: bool
    recorded: bool = False
    note: str | None = None


@dataclass
class FollowupResult:
    review_id: str
    base: str
    top: str
    items: list[FollowupItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def followup(ws: Workspace, review_id: str, to: str = "HEAD", dry_run: bool = False) -> FollowupResult:
    review_dir = ws.reviews_dir / review_id
    packet_path = review_dir / "packet.json"
    if not packet_path.exists():
        raise FileNotFoundError(f"no review '{review_id}'")
    meta = json.loads(packet_path.read_text(encoding="utf-8")).get("metadata", {})
    reviewed = str(meta.get("reviewed") or "")
    if meta.get("committed") != "true" or not _SHA.match(reviewed):
        raise ValueError(
            f"review {review_id} was not made on a committed range; follow-ups need `review --diff A..B` so the reviewed commit is known"
        )
    repo = Path(meta.get("repo") or ws.project_dir)
    if not (repo / ".git").exists():
        raise ValueError(f"{repo} is not a git repository; the review's packet names a repo that is not here")
    base = reviewed
    diff_text, top = git_diff_text(repo, base, to)
    result = FollowupResult(review_id=review_id, base=base, top=top)
    if base != top and not is_ancestor(repo, base, top):
        raise ValueError(
            f"{to} ({top[:7]}) does not descend from the reviewed commit {base[:7]}; a diff across branches would touch lines for reasons unrelated to the findings. Check out a descendant or pass --to"
        )
    if base == top:
        result.notes.append("nothing has been committed since the reviewed commit")
        return result
    touched = touched_ranges(diff_text)
    config = ws.load_config()
    store = MemoryStore(ws)
    open_decisions = [d for d in store.load_decisions() if d.review_id == review_id and d.outcome is None and not d.withdrawn]
    if not open_decisions:
        result.notes.append("no open decisions for this review")
    for d in open_decisions:
        where = d.where or ""
        item = FollowupItem(decision_id=d.id, persona=d.persona, where=where, claim=d.claim, touched=False)
        if d.kind not in CLOSABLE_KINDS:
            item.note = f"kind '{d.kind}' is not closable by a commit; record it by hand"
            result.items.append(item)
            continue
        cited = cited_ranges(where)
        if cited is None:
            item.note = "cited location is not path:line, cannot match"
            result.items.append(item)
            continue
        path, ranges = cited
        span = sum(b - a + 1 for a, b in ranges)
        if span > config.outcomes.followup_max_lines:
            item.note = f"cites {span} lines, too broad to tell whether a later change addressed it; record it by hand"
            result.items.append(item)
            continue
        item.touched = ranges_touched(path, ranges, touched)
        if item.touched and not dry_run:
            try:
                record_outcome(
                    ws,
                    d.id,
                    True,
                    note=f"lines {where} changed between {base[:7]} and {top[:7]}",
                    learning_rate=config.outcomes.commit_learning_rate,
                    source=f"implicit:commit:{top[:7]}",
                )
                item.recorded = True
            except (KeyError, ValueError) as exc:
                item.note = str(exc)
        result.items.append(item)
    return result
