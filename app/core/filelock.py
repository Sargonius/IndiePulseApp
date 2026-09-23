"""Tiny cross-process lock (the web server and the Huey worker are separate processes).

Uses an O_EXCL lock file, so it also serialises threads inside one process. A lock
file older than `stale` seconds is assumed to belong to a crashed process and is
broken.
"""
from __future__ import annotations

import os
import time

LOCK_DIR = ".locks"


class FileLock:
    def __init__(self, name: str, timeout: float = 30.0, stale: float = 120.0) -> None:
        os.makedirs(LOCK_DIR, exist_ok=True)
        self.path = os.path.join(LOCK_DIR, f"{name}.lock")
        self.timeout = timeout
        self.stale = stale
        self._fd: int | None = None

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return
            except (FileExistsError, PermissionError):
                # Windows reports a lock file that is mid-deletion as PermissionError
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass  # holder released it between our calls — just try again
                if time.monotonic() > deadline:
                    raise TimeoutError(f"lock busy: {self.path}")
                time.sleep(0.05)

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
                try:
                    os.remove(self.path)
                except OSError:
                    pass

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
