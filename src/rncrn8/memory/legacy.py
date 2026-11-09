"""Workspaces from before the rename.

rncrn8 was called reincarnate before its first release and kept each project's workspace in
`.reincarnate/`. A workspace found under that name is renamed to `.rncrn8/` in place the first time
any command uses it, so everything recorded in it carries over, and the new name is kept out of git
the way setup keeps it out. It is left where it is, and still used, when git tracks it (a rename
would show up as deleted files in the next commit; `git mv` does it properly) or when the rename
fails, for example because another program has a file in it open.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

LEGACY_WORKSPACE_DIRS = (".reincarnate",)


def git(root: Path, *args: str) -> str | None:
    """stdout of a git command run in `root`, or None when git is missing or the command fails."""
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotePath=false", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def tracked(project: Path, dirname: str) -> bool:
    """Whether git tracks any file under `project/dirname`."""
    return bool(git(project, "ls-files", "--", dirname))


def git_standing(project: Path, dirname: str) -> str:
    """How git treats the workspace folder, in words: tracked, kept out and by which file, or not ignored."""
    if git(project, "rev-parse", "--is-inside-work-tree") != "true":
        return "not in a git repository"
    if tracked(project, dirname):
        return f"tracked: git has files from {dirname}/, so they go into commits"
    rule = git(project, "check-ignore", "-v", f"{dirname}/")
    if rule:
        return f"kept out of git by {rule.split(':', 1)[0]}"
    return f"not ignored: a broad git add would commit {dirname}/; `rncrn8 init` adds it to .git/info/exclude"


def keep_out_of_git(project: Path, dirname: str) -> bool:
    """Add `dirname/` to .git/info/exclude unless git already ignores it. True when a line was added."""
    if git(project, "rev-parse", "--is-inside-work-tree") != "true":
        return False
    if git(project, "check-ignore", "-q", f"{dirname}/") is not None:
        return False
    rel = git(project, "rev-parse", "--git-path", "info/exclude")
    if not rel:
        return False
    path = Path(rel) if Path(rel).is_absolute() else project / rel
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(existing + ("" if not existing or existing.endswith("\n") else "\n") + f"{dirname}/\n", encoding="utf-8")
    except OSError:
        return False
    return True


def adopt(old: Path, new_name: str) -> tuple[Path, bool]:
    """Rename a workspace found under its old name to `new_name`, next to it.

    Returns the root to use and whether it moved. It stays where it is when git tracks it, when
    something already has the new name, or when the rename fails; it is still used from there."""
    project, new = old.parent, old.parent / new_name
    if new.exists() or tracked(project, old.name):
        return old, False
    try:
        old.rename(new)
    except OSError:
        return old, False
    keep_out_of_git(project, new_name)
    return new, True
