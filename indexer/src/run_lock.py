"""Whole-run mutual exclusion via ``flock`` on ``<data dir>/.indexer.lock``.

The scheduled run, a manual ``python -m src.snapshots build`` and an
accidentally started second container all share the data directory, so the
lock keeps exactly one of them touching the git checkout and the database.
The lock is non-blocking: a run that cannot take it records ``skipped_busy``
and exits rather than queueing.
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import paths


class RunLockBusy(Exception):
    """Another indexer run holds the lock."""


@contextmanager
def run_lock(path: Path | None = None) -> Iterator[None]:
    path = path or paths.LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunLockBusy(f"another indexer run holds {path}") from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
