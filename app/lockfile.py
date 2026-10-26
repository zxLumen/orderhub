"""Single-instance guard.

Two servers would each own a scheduler and a JD collector: duplicate collections,
two ``_RUN_LOCK`` instances that never see each other, and needless database
contention. On Windows a second bind can even succeed because HTTPServer sets
SO_REUSEADDR, so the port alone is not a reliable guard.

The lock is an OS-level exclusive lock on ``data/orderhub.lock``; the pid is
written to ``orderhub.lock.pid`` (a separate file so the holder's pid stays
readable while the lock byte itself is locked). The OS releases the lock when
the process dies, so a crash never leaves a stale lock.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO, TextIO


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.pid_path = self.path.with_name(self.path.name + ".pid")
        self._handle: TextIO | None = None

    def acquire(self) -> bool:
        """Try to take the lock. False means another instance is running."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle: IO[str] = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write("\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False

        try:
            self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
        self._handle = handle
        return True

    def owner(self) -> str:
        """PID recorded by the current holder (best effort)."""
        try:
            return self.pid_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()
            self._handle = None
            try:
                self.pid_path.unlink()
            except OSError:
                pass
