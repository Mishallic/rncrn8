from pathlib import Path

import pytest

import rncrn8.models.claude as _claude_models
from rncrn8.memory import Workspace
from rncrn8.persona import scaffold_persona

_REAL_CREDENTIALS_FOUND = _claude_models.credentials_found


@pytest.fixture
def real_credentials_found():
    """The SDK-backed check itself, for the one test that exercises it."""
    return _REAL_CREDENTIALS_FOUND


@pytest.fixture(autouse=True)
def no_real_claude_code(monkeypatch):
    """Tests never read or change the Claude Code installed on this machine; test_connect brings its own."""
    monkeypatch.setattr("rncrn8.connect.find_claude", lambda: None)


@pytest.fixture(autouse=True)
def credentials_only_from_the_environment(monkeypatch):
    """An `ant auth login` profile, a key or a workspace id on the machine running the tests must not change what
    they see: credentials count as found exactly when a test sets RNCRN8_API_KEY, ANTHROPIC_API_KEY or
    ANTHROPIC_AUTH_TOKEN, and a workspace id is sent only when a test sets one."""
    for name in ("RNCRN8_API_KEY", "RNCRN8_WORKSPACE_ID", "ANTHROPIC_WORKSPACE_ID"):
        monkeypatch.delenv(name, raising=False)
    import os

    monkeypatch.setattr("rncrn8.models.claude.credentials_found",
                        lambda: bool(os.environ.get("RNCRN8_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")))


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    workspace = Workspace.init(tmp_path)
    scaffold_persona(workspace, "critic", template="critic")
    scaffold_persona(workspace, "skimmer", template="skimmer")
    return workspace
