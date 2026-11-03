"""The workspace's client lock: only one set of GTNH clients runs on the machine at a time.

`WS/locks/client.lock` is held with `flock` (released by the kernel when the holder exits, also on a crash) for the
whole of every client-launching command (`run`, `play`, `export`, `cameras`, `states`; one hold for all workers of a
`--parallel` run). The holder writes who it is into the file (pid, command, start time, cwd); a command that finds the
lock taken prints that and waits until it is free. `BMGTNH_CLIENT_LOCK_WAIT=<seconds>` bounds the wait (`0`: fail at
once instead of queueing).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import sys
import time
from pathlib import Path

LOCK_DIR = "locks"
CLIENT_LOCK = "client.lock"
WAIT_ENV = "BMGTNH_CLIENT_LOCK_WAIT"
POLL_SECONDS = 1.0

_held: dict[Path, int] = {}  # lock path -> nesting depth of this process's hold


class LockBusy(Exception):
    """The lock is held by another process and the wait bound (``BMGTNH_CLIENT_LOCK_WAIT``) ran out."""


def lock_path(ws: Path) -> Path:
    return ws / LOCK_DIR / CLIENT_LOCK


def read_holder(path: Path) -> dict | None:
    """The holder record of a lock file, or None when it is empty or unreadable (free, or just being taken)."""
    try:
        text = path.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else None
    except (OSError, ValueError):
        return None


def describe_holder(holder: dict | None) -> str:
    if not holder:
        return "an unknown process (no holder record yet)"
    return (f"pid {holder.get('pid')} on {holder.get('host')} ({holder.get('command')}, since {holder.get('started')}, "
            f"cwd {holder.get('cwd')})")


def _wait_limit(wait: float | None) -> float | None:
    if wait is not None:
        return wait
    value = os.environ.get(WAIT_ENV, "").strip()
    return float(value) if value else None


@contextlib.contextmanager
def client_lock(ws: Path, describe: str | None = None, *, wait: float | None = None):
    """Holds ``WS/locks/client.lock`` for the ``with`` block. ``describe`` names the holder for waiting processes
    (default: the command line). ``wait``: seconds to wait for a busy lock (None: ``BMGTNH_CLIENT_LOCK_WAIT``, else
    forever; 0: raise ``LockBusy`` at once). Re-entrant within one process."""
    path = lock_path(ws).resolve()
    if path in _held:  # nested use in the same process: the outer hold covers it
        _held[path] += 1
        try:
            yield path
        finally:
            _held[path] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        limit = _wait_limit(wait)
        started = time.monotonic()
        reported = None
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                holder = describe_holder(read_holder(path))
                if limit is not None and time.monotonic() - started >= limit:
                    raise LockBusy(f"client lock {path} is held by {holder}") from None
                if holder != reported:
                    print(f"[lock] client lock {path} is held by {holder}; waiting", file=sys.stderr, flush=True)
                    reported = holder
                time.sleep(POLL_SECONDS if limit is None else min(POLL_SECONDS, max(0.0, limit - (time.monotonic() - started))))
        if reported is not None:
            print(f"[lock] client lock acquired after {time.monotonic() - started:.0f}s", file=sys.stderr, flush=True)
        record = {"pid": os.getpid(), "host": socket.gethostname(),
                  "command": describe or " ".join([Path(sys.argv[0]).name, *sys.argv[1:]]),
                  "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "cwd": os.getcwd()}
        os.ftruncate(fd, 0)
        os.pwrite(fd, (json.dumps(record) + "\n").encode("utf-8"), 0)
        _held[path] = 1
        try:
            yield path
        finally:
            del _held[path]
            os.ftruncate(fd, 0)  # free: no stale holder record for the next waiter
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
