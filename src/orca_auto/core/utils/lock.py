from __future__ import annotations

import fcntl
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from .persistence import now_utc_iso, open_pinned_readonly

_MAX_LOCK_PAYLOAD_BYTES = 16 * 1024


class FileLockTimeoutError(TimeoutError):
    """Raised only when a file lock remains contended through its deadline."""


@contextmanager
def file_lock_at(
    directory_fd: int,
    lock_name: str,
    *,
    display_path: Path | None = None,
    timeout_seconds: float = 10.0,
    payload: str | None = None,
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
                    raise FileLockTimeoutError(f"Timed out acquiring lock: {shown_path}") from None
                time.sleep(0.1)

        handle.seek(0)
        handle.truncate()
        lock_payload = (
            payload if payload is not None else f"pid={os.getpid()}\nacquired_at={now_utc_iso()}"
        )
        # The payload is diagnostic only: ``held_file_lock_payload`` reads it
        # through the shared page cache while the flock is held, and the flock
        # itself vanishes with its owner, so the bytes never need to survive a
        # crash. Flush for other processes; do not fsync on every acquisition.
        handle.write(lock_payload.rstrip("\n") + "\n")
        handle.flush()
        try:
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(
    lock_path: Path,
    *,
    timeout_seconds: float = 10.0,
    payload: str | None = None,
) -> Iterator[None]:
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
            payload=payload,
        ):
            yield
    finally:
        os.close(directory_fd)


def held_file_lock_payload(lock_path: Path) -> str | None:
    """Return a held lock's short payload, or ``None`` when no lock is held."""
    if lock_path.parent.is_symlink():
        raise ValueError(f"Lock directory must not be a symlink: {lock_path.parent}")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_fd = os.open(lock_path.parent, directory_flags)
    except FileNotFoundError:
        return None
    descriptor = -1
    try:
        try:
            descriptor = open_pinned_readonly(lock_path.name, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ValueError(f"Lock path must be a single-link regular file: {lock_path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            payload = os.read(descriptor, _MAX_LOCK_PAYLOAD_BYTES + 1)
            if len(payload) > _MAX_LOCK_PAYLOAD_BYTES:
                raise ValueError(f"Lock payload is too large: {lock_path}") from None
            return payload.decode("utf-8", errors="strict")
        else:
            with suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)
