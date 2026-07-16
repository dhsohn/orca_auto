"""Shared dirfd-anchored safety primitives for the smoke package.

The manifest, runner, and review modules all pin directories and files by
descriptor, verify identities before and after use, and read regular files
with change detection. This module holds the single implementation of those
checks; callers keep their own error types, messages, and tolerance policies.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass

_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class DirectoryIdentity:
    device: int
    inode: int


class PinnedReadError(ValueError):
    """A pinned regular file failed verification; ``reason`` says how."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def directory_open_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def file_open_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def stat_identity(status: os.stat_result) -> tuple[int, int, int, int]:
    return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)


def directory_identity(
    directory_fd: int,
    *,
    label: str,
    error: type[Exception] = ValueError,
) -> DirectoryIdentity:
    details = os.fstat(directory_fd)
    if not stat.S_ISDIR(details.st_mode):
        raise error(f"{label} is not a directory")
    return DirectoryIdentity(details.st_dev, details.st_ino)


def validate_owned_directory(directory_fd: int, *, label: str) -> DirectoryIdentity:
    details = os.fstat(directory_fd)
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError(f"{label} must be a directory")
    if details.st_uid != os.geteuid():
        raise ValueError(f"{label} must be owned by the current user")
    if stat.S_IMODE(details.st_mode) & 0o022:
        raise ValueError(f"{label} must not be group- or world-writable")
    return DirectoryIdentity(details.st_dev, details.st_ino)


def open_named_directory_identity(
    parent_fd: int,
    name: str,
    expected: DirectoryIdentity,
    *,
    label: str,
    error: type[Exception] = ValueError,
) -> int:
    try:
        descriptor = os.open(name, directory_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise error(f"{label} identity changed") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISDIR(details.st_mode):
            raise error(f"{label} identity changed")
        if DirectoryIdentity(details.st_dev, details.st_ino) != expected:
            raise error(f"{label} identity changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def assert_named_directory_identity(
    parent_fd: int,
    name: str,
    expected: DirectoryIdentity,
    *,
    label: str,
    error: type[Exception] = ValueError,
) -> None:
    os.close(open_named_directory_identity(parent_fd, name, expected, label=label, error=error))


def read_pinned_regular_file(
    descriptor: int,
    *,
    max_bytes: int,
    expected_identity: tuple[int, int, int, int] | None = None,
    named_location: tuple[int, str] | None = None,
    require_owner_private: bool = False,
) -> bytes:
    """Read the whole regular file behind ``descriptor`` with change detection.

    Raises PinnedReadError with a reason of ``not-regular``, ``multi-link``,
    ``not-owner-private``, ``too-large``, ``changed-before``, ``short-read``,
    or ``changed-during``. Callers translate reasons into their own errors.
    """

    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise PinnedReadError("not-regular")
    if before.st_nlink != 1:
        raise PinnedReadError("multi-link")
    if require_owner_private and (
        before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o077
    ):
        raise PinnedReadError("not-owner-private")
    if before.st_size > max_bytes:
        raise PinnedReadError("too-large")
    identity = stat_identity(before)
    if expected_identity is not None and identity != expected_identity:
        raise PinnedReadError("changed-before")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = before.st_size
    while remaining:
        chunk = os.read(descriptor, min(_CHUNK_BYTES, remaining))
        if not chunk:
            raise PinnedReadError("short-read")
        chunks.append(chunk)
        remaining -= len(chunk)
    after = os.fstat(descriptor)
    if stat_identity(after) != identity or after.st_nlink != 1:
        raise PinnedReadError("changed-during")
    if named_location is not None:
        parent_fd, name = named_location
        try:
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise PinnedReadError("changed-during") from exc
        if stat_identity(named) != identity or named.st_nlink != 1:
            raise PinnedReadError("changed-during")
    return b"".join(chunks)


def assert_named_regular_file(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int, int, int],
) -> None:
    """Verify that ``name`` still is the pinned single-link regular file."""

    descriptor = os.open(name, file_open_flags(), dir_fd=parent_fd)
    try:
        verified = os.fstat(descriptor)
        if (
            not stat.S_ISREG(verified.st_mode)
            or verified.st_nlink != 1
            or stat_identity(verified) != expected_identity
        ):
            raise PinnedReadError("changed-during")
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat_identity(named) != expected_identity or named.st_nlink != 1:
            raise PinnedReadError("changed-during")
    finally:
        os.close(descriptor)
