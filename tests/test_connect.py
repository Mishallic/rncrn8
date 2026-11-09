"""`rncrn8 init` connects Claude Code for every project, through a stand-in for its CLI: these tests never
touch the Claude Code installed on the machine."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from rncrn8.cli import main
from rncrn8.doctor import run_checks
from rncrn8.memory import Workspace

FAKE = """import json, os, sys
log = os.environ["FAKE_CLAUDE_LOG"]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["mcp", "get"]:
    sys.exit(0 if os.environ.get("FAKE_CLAUDE_HAS") == "1" else 1)
if sys.argv[1:3] == ["mcp", "add"]:
    print("Added stdio MCP server rncrn8 to user config")
    sys.exit(0)
sys.exit(2)
"""


@pytest.fixture
def claude(tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE, encoding="utf-8")
    log = tmp_path / "claude.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setattr("rncrn8.connect.find_claude", lambda: [sys.executable, str(script)])
    calls = lambda: [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return calls


def test_init_connects_claude_code_for_every_project(tmp_path, monkeypatch, claude):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test-only")
    res = CliRunner().invoke(main, ["init", "--yes"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert claude() == [["mcp", "get", "rncrn8"], ["mcp", "add", "--scope", "user", "rncrn8", "--", sys.executable, "-m", "rncrn8", "mcp"]]
    assert "Claude Code: connected for all your projects, in the terminal and the VS Code extension" in res.output and "No Anthropic key" not in res.output
    assert "next: open Claude Code in this project and type /rncrn8:setup" in res.output
    assert (tmp_path / ".rncrn8" / "config.yaml").exists()


def test_init_asks_first_and_a_no_connects_nothing(tmp_path, monkeypatch, claude):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    res = CliRunner().invoke(main, ["init"], input="n\n", catch_exceptions=False)
    assert res.exit_code == 0 and claude() == [["mcp", "get", "rncrn8"]]
    assert "connect rncrn8 to Claude Code, for all your projects? [Y/n]" in res.output and "not connected" in res.output
    assert "next: connect Claude Code, with `rncrn8 init` again or the command above" in res.output
    assert "claude mcp add --scope user rncrn8 --" in res.output and "No Anthropic key found" in res.output
    assert 'setx RNCRN8_API_KEY "sk-ant-api03-..."' in res.output and "Claude Code keeps using your Claude login" in res.output


def test_init_leaves_an_existing_connection_alone(tmp_path, monkeypatch, claude):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAKE_CLAUDE_HAS", "1")
    res = CliRunner().invoke(main, ["init"], catch_exceptions=False)
    assert "Claude Code: rncrn8 is already connected" in res.output and claude() == [["mcp", "get", "rncrn8"]]


def test_init_without_claude_code_prints_what_to_paste(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(main, ["init"], catch_exceptions=False)
    assert "Claude Code was not found on this PATH" in res.output
    assert "Claude Code, in a terminal or the VS Code extension: claude mcp add --scope user rncrn8 --" in res.output
    entry = json.loads(next(line for line in res.output.splitlines() if '"mcpServers"' in line))
    server = entry["mcpServers"]["rncrn8"]
    assert server["command"] == sys.executable and server["args"][:4] == ["-m", "rncrn8", "mcp", "--workspace"]
    # other assistants start wherever they like, so the project is named by its full path, never "."
    assert Path(server["args"][4]).is_absolute() and Path(server["args"][4]) == tmp_path.resolve()


def test_doctor_says_whether_the_assistant_is_connected(tmp_path, monkeypatch, claude):
    ws = Workspace.init(tmp_path)
    assert next(c for c in run_checks(ws) if c.name == "assistant").level == "warn"
    monkeypatch.setenv("FAKE_CLAUDE_HAS", "1")
    check = next(c for c in run_checks(ws) if c.name == "assistant")
    assert check.level == "ok" and "/rncrn8:review" in check.detail


def test_python_dash_m_runs_the_command_line():
    out = subprocess.run([sys.executable, "-m", "rncrn8", "--version"], capture_output=True, text=True, check=True).stdout
    from rncrn8 import __version__

    assert out.strip() == f"rncrn8, version {__version__}"


def test_credentials_are_found_the_way_the_sdk_finds_them(tmp_path, monkeypatch, real_credentials_found):
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path))  # no `ant auth login` profile on disk here
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert real_credentials_found() is False
    monkeypatch.setenv("RNCRN8_API_KEY", "sk-ant-api03-for-rncrn8-only")
    assert real_credentials_found() is True  # rncrn8's own variable, which Claude Code does not read
    monkeypatch.delenv("RNCRN8_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test-only")
    assert real_credentials_found() is True


def test_rncrn8s_own_key_comes_first_and_doctor_says_where_the_key_is(tmp_path, monkeypatch):
    from rncrn8.models.claude import ClaudeAdapter

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-claude-code-would-use-this")
    monkeypatch.setenv("RNCRN8_API_KEY", "sk-ant-api03-for-rncrn8-only")
    assert ClaudeAdapter().client.api_key == "sk-ant-api03-for-rncrn8-only"
    ws = Workspace.init(tmp_path)
    key = next(c for c in run_checks(ws) if c.name == "api key")
    assert key.level == "ok" and "in RNCRN8_API_KEY, which only rncrn8 reads" in key.detail and "bills its own use" not in key.detail
    monkeypatch.delenv("RNCRN8_API_KEY")
    key = next(c for c in run_checks(ws) if c.name == "api key")
    assert "bills its own use to it instead of a Claude subscription" in key.detail  # only ANTHROPIC_API_KEY: say what it costs


def test_init_in_a_project_that_has_a_panel_points_to_a_review(tmp_path, monkeypatch, claude):
    from rncrn8.persona import scaffold_persona

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FAKE_CLAUDE_HAS", "1")
    scaffold_persona(Workspace.init(tmp_path), "tester", template="tester")
    res = CliRunner().invoke(main, ["init"], catch_exceptions=False)
    assert "next: open Claude Code in this project and ask for a review, or type /rncrn8:review" in res.output


def test_doctor_reports_the_workspace_id_it_will_send(tmp_path, monkeypatch):
    monkeypatch.setenv("RNCRN8_API_KEY", "sk-ant-usr-test-only")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_example")
    monkeypatch.delenv("RNCRN8_WORKSPACE_ID", raising=False)
    check = next(c for c in run_checks(Workspace.init(tmp_path)) if c.name == "api workspace")
    assert check.level == "ok" and check.detail.startswith("wrkspc_example from ANTHROPIC_WORKSPACE_ID")
