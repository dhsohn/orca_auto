from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from .persistence import now_utc_iso


@contextmanager
def file_lock(lock_path: Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    with lock_path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring lock: {lock_path}") from None
                time.sleep(0.1)

        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\nacquired_at={now_utc_iso()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        try:
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
