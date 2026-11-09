"""Connecting rncrn8 to the assistant the user works in, so that after `rncrn8 init` everything happens in the chat.

Claude Code gets the server registered once, for every project (`claude mcp add --scope user`), run by
the same Python that rncrn8 is installed in, so it does not depend on what is on PATH when Claude Code
starts. The server finds each project's workspace from the folder Claude Code has open. Other
assistants get the entry to paste into their MCP settings.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

SERVER_NAME = "rncrn8"


def find_claude() -> list[str] | None:
    """The command that runs Claude Code's CLI, or None when it is not installed."""
    found = shutil.which("claude")
    return [found] if found else None


def server_command() -> list[str]:
    return [sys.executable, "-m", "rncrn8", "mcp"]


def _run(cmd: list[str], timeout: float = 90) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return r.returncode, (r.stdout + r.stderr).strip()


def registered(claude: list[str]) -> bool:
    """Whether Claude Code already knows a server named rncrn8, in any scope."""
    code, _ = _run([*claude, "mcp", "get", SERVER_NAME])
    return code == 0


def register(claude: list[str]) -> tuple[bool, str]:
    """Register the server with Claude Code for every project. Returns (done, what Claude Code said)."""
    code, said = _run([*claude, "mcp", "add", "--scope", "user", SERVER_NAME, "--", *server_command()])
    return code == 0, said


def _shown(arg: str) -> str:
    return f'"{arg}"' if any(c.isspace() for c in arg) else arg


def manual_steps(project: Path) -> list[str]:
    """How to connect by hand: the Claude Code command, which serves its terminal and its VS Code extension
    alike, and the entry other MCP clients take, naming the project by its full path: a relative one would be
    read from wherever that client starts."""
    cmd = server_command()
    entry = {"mcpServers": {SERVER_NAME: {"command": cmd[0], "args": [*cmd[1:], "--workspace", str(Path(project).resolve())]}}}
    return [
        "Claude Code, in a terminal or the VS Code extension: claude mcp add --scope user rncrn8 -- " + " ".join(_shown(c) for c in cmd),
        "Other MCP clients, such as Cursor: add this server entry to their MCP settings",
        "  " + json.dumps(entry),
    ]
