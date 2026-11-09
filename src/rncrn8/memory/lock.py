"""A small cross-platform file lock for read-modify-write of the shared JSON state.

The CLI and the MCP server may run at the same time in one workspace. Append-only logs are
safe to share, but reliability.json and panel.json are rewritten whole, so every update to
them happens under this lock. Implemented with an exclusive-create lock file, which works on
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
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > stale_after:
                try:
                    lock.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() > deadline:
                raise LockTimeout(f"could not lock {path} within {timeout}s; is another rncrn8 stuck? remove {lock} if so")
            time.sleep(0.01)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
