from __future__ import annotations

import fcntl
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from .persistence import now_utc_iso


@contextmanager
def file_lock_at(
    directory_fd: int,
    lock_name: str,
    *,
    display_path: Path | None = None,
    timeout_seconds: float = 10.0,
) -> Iterator[None]:
    """Lock one single-link regular file relative to an already-pinned directory."""

    if not lock_name or Path(lock_name).name != lock_name or lock_name in {".", ".."}:
        raise ValueError(f"Lock name must be one plain filename: {lock_name!r}")
    shown_path = display_path or Path(lock_name)
    deadline = time.monotonic() + timeout_seconds
    file_flags = os.O_RDWR | os.O_CREAT
    file_flags |= os.O_NONBLOCK
    file_flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_name, file_flags, 0o600, dir_fd=directory_fd)
    file_status = os.fstat(descriptor)
    if not stat.S_ISREG(file_status.st_mode) or file_status.st_nlink != 1:
        os.close(descriptor)
        raise ValueError(f"Lock path must be a single-link regular file: {shown_path}")
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring lock: {shown_path}") from None
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


@contextmanager
def file_lock(lock_path: Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    if lock_path.parent.is_symlink():
        raise ValueError(f"Lock directory must not be a symlink: {lock_path.parent}")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    directory_flags = os.O_RDONLY
    directory_flags |= os.O_DIRECTORY
    directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(lock_path.parent, directory_flags)
    try:
        with file_lock_at(
            directory_fd,
            lock_path.name,
            display_path=lock_path,
            timeout_seconds=timeout_seconds,
        ):
            yield
    finally:
        os.close(directory_fd)
