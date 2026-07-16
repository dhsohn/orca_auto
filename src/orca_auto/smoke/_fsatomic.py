"""One dirfd-anchored atomic write shared by the smoke manifest and review packet.

Both surfaces publish through an exclusive no-follow staging file that is
fsynced, sanity-checked, and renamed over the target. Smoke outputs are
regenerable from the retained runtime tree, so a failed publication is
reported as a failure instead of being rolled back to the previous surface.
"""

from __future__ import annotations

import os
import stat
from secrets import token_hex


def atomic_write_bytes_at(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    mode: int = 0o644,
    error: type[Exception] = ValueError,
) -> None:
    temporary = f".{name}.{token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, mode, dir_fd=directory_fd)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            details = os.fstat(handle.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise error("atomic staging file is unsafe")
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            # After a successful rename the staging name no longer exists; any
            # other leftover is best-effort cleanup and never a false failure.
            pass


__all__ = ["atomic_write_bytes_at"]
