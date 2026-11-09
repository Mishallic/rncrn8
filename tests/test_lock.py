"""The file lock behaves on Windows quirks we can simulate anywhere."""

import os

import pytest

from rncrn8.memory import lock as lockmod


def test_a_lock_being_deleted_is_waited_for_not_an_error(tmp_path, monkeypatch):
    real_open = os.open
    calls = {"n": 0}

    def flaky_open(path, flags, *args):
        calls["n"] += 1
        if calls["n"] <= 2:  # Windows: the holder is still deleting the lock file
            raise PermissionError(13, "Access is denied")
        return real_open(path, flags, *args)

    monkeypatch.setattr(lockmod.os, "open", flaky_open)
    with lockmod.locked(tmp_path / "state", timeout=2):
        assert (tmp_path / "state.lock").exists()
    assert not (tmp_path / "state.lock").exists() and calls["n"] == 3


def test_a_lasting_permission_error_is_raised_at_the_deadline(tmp_path, monkeypatch):
    def denied(path, flags, *args):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(lockmod.os, "open", denied)
    with pytest.raises(PermissionError):
        with lockmod.locked(tmp_path / "state", timeout=0.05):
            pass


def test_releasing_retries_a_delete_another_process_briefly_blocks(tmp_path, monkeypatch):
    real_unlink = lockmod.Path.unlink
    calls = {"n": 0}

    def flaky_unlink(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "The process cannot access the file")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(lockmod.Path, "unlink", flaky_unlink)
    with lockmod.locked(tmp_path / "state", timeout=2):
        pass
    assert not (tmp_path / "state.lock").exists() and calls["n"] == 3
