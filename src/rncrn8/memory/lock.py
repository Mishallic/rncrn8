"""A small cross-platform file lock for read-modify-write of the shared JSON state.

The CLI and the MCP server may run at the same time in one workspace. reliability.json and
panel.json are rewritten whole, so every update to them happens under this lock. Appends to the
JSONL logs take it too: on Windows, append mode is a seek and then a write, not one atomic step,
so concurrent appends without it can overwrite each other. Implemented with an exclusive-create lock file, which works on
every file system Python runs on; a lock older than `stale_after` seconds is assumed dead.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path


class LockTimeout(RuntimeError):
    pass


@contextmanager
def locked(path: Path, timeout: float = 10.0, stale_after: float = 60.0):
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except (FileExistsError, PermissionError) as exc:
            # On Windows a lock file its holder is still deleting reads as PermissionError, not as existing:
            # wait for it like any held lock. A PermissionError that lasts to the deadline is a real one.
            if time.monotonic() > deadline:
                if isinstance(exc, PermissionError):
                    raise
                raise LockTimeout(f"could not lock {path} within {timeout}s; is another rncrn8 stuck? remove {lock} if so") from None
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            except PermissionError:
                time.sleep(0.01)
                continue
            if age > stale_after:
                _remove(lock)
                continue
            time.sleep(0.01)
    try:
        yield
    finally:
        _remove(lock)


def _remove(lock: Path) -> None:
    """Delete the lock file. On Windows a delete fails while another process briefly has the file open
    (to check its age, or an antivirus scan), so retry for a moment; a lock left behind goes stale."""
    for _ in range(100):
        try:
            lock.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.01)
