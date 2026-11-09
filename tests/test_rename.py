"""A workspace from before the tool was renamed rncrn8 keeps working: renamed on first use when it can
be, used where it is when git tracks it, and never split into two."""

import subprocess
from pathlib import Path

from click.testing import CliRunner

from rncrn8.cli import main
from rncrn8.config import SourceType
from rncrn8.doctor import run_checks
from rncrn8.memory import MemoryStore, Workspace
from rncrn8.service import remember


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
                          check=True, capture_output=True, text=True).stdout


def _old_workspace(project: Path) -> Path:
    """A workspace as setup left it before the rename: .reincarnate/, kept out of git by .git/info/exclude."""
    project.mkdir(parents=True)
    _git(project, "init", "-q")
    (project / "README.md").write_text("# p\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "base")
    ws = Workspace.init(project)
    remember(ws, "shared", "Launch is fixed for next week", SourceType.HUMAN, "planning call", who="founder")
    ws.root.rename(project / ".reincarnate")
    (project / ".git" / "info" / "exclude").write_text(".reincarnate/\n", encoding="utf-8")
    return project / ".reincarnate"


def test_an_old_workspace_is_renamed_on_first_use_and_stays_out_of_git(tmp_path):
    project = tmp_path / "proj"
    old = _old_workspace(project)
    (project / "src").mkdir()
    ws = Workspace.find(project / "src")
    assert ws.root == project / ".rncrn8" and ws.renamed_from == old and not old.exists() and not ws.old_name
    assert [e.claim for e in MemoryStore(ws).load("shared")] == ["Launch is fixed for next week"]
    assert ".rncrn8/" in (project / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert _git(project, "status", "--porcelain") == ""  # still nothing of the workspace to commit
    again = Workspace.find(project)
    assert again.root == ws.root and again.renamed_from is None  # renamed once, said once


def test_an_old_workspace_git_tracks_is_used_where_it_is_and_doctor_says_how_to_rename_it(tmp_path):
    project = tmp_path / "proj"
    old = _old_workspace(project)
    (project / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
    _git(project, "add", ".reincarnate")
    _git(project, "commit", "-qm", "the workspace")
    ws = Workspace.find(project)
    assert ws.root == old and ws.renamed_from is None and ws.old_name  # renaming would delete tracked files in the next commit
    assert [e.claim for e in MemoryStore(ws).load("shared")] == ["Launch is fixed for next week"]
    check = next(c for c in run_checks(ws) if c.name == "workspace")
    assert check.level == "warn" and "`git mv .reincarnate .rncrn8`" in check.detail


def test_init_takes_over_an_old_workspace_instead_of_starting_a_second(tmp_path):
    project = tmp_path / "proj"
    old = _old_workspace(project)
    ws = Workspace.init(project)
    assert ws.root == project / ".rncrn8" and ws.renamed_from == old and not old.exists()
    assert len(MemoryStore(ws).load("shared")) == 1


def test_a_workspace_under_the_new_name_wins_and_the_old_one_is_left_alone(tmp_path):
    project = tmp_path / "proj"
    old = _old_workspace(project)
    Workspace.init(project / "elsewhere").root.rename(project / ".rncrn8")  # both names in one folder
    ws = Workspace.find(project)
    assert ws.root == project / ".rncrn8" and ws.renamed_from is None and old.is_dir()


def test_the_command_line_says_on_stderr_that_it_renamed_the_workspace(tmp_path, monkeypatch):
    _old_workspace(tmp_path / "proj")
    monkeypatch.chdir(tmp_path / "proj")
    first = CliRunner().invoke(main, ["decisions"], catch_exceptions=False)
    assert first.exit_code == 0 and "no decisions" in first.stdout
    assert "renamed " in first.stderr and "the tool is now called rncrn8" in first.stderr and "renamed" not in first.stdout
    second = CliRunner().invoke(main, ["decisions"], catch_exceptions=False)
    assert second.exit_code == 0 and "renamed" not in second.stderr
