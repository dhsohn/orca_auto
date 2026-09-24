"""Pinned, no-follow filesystem primitives shared by fd-safe consumers.

Every helper pins one inode through a descriptor and refuses to continue when
the pathname stops naming that inode. Semantic failures raise
:class:`StableFsError` with a ``reason`` so each consumer translates them to its
own error type and wording at its boundary; ``OSError`` raised by the underlying
system calls (missing paths, ``ENOTDIR``, permission errors) propagates
unchanged so consumers keep their existing handling of those.

Directory opens keep the strictest check of the two former copies. The
``input_snapshot`` copy was stricter on identity: the inode observed by
``lstat`` before the open, the opened descriptor and the pathname re-checked
after the open must all agree, and must equal ``expected_identity`` when one is
given (the ``engine_scratch`` copy compared only the descriptor with the
pathname after the open). The ``engine_scratch`` copy was stricter on entry
names: a child opened relative to a parent descriptor must be a plain basename
(no separators, not ``.`` or ``..``). Both rules apply to every open here.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Literal

from .persistence import open_pinned_readonly

DEFAULT_READ_CHUNK_BYTES = 1024 * 1024

StableFsReason = Literal[
    "unsafe_name",
    "missing",
    "symlink",
    "not_directory",
    "unexpected_identity",
    "identity_changed",
    "not_regular",
    "not_single_link",
    "too_large",
    "changed",
    "short_write",
]


class StableFsError(Exception):
    """A pinned filesystem object failed a safety or stability check.

    ``reason`` names the failed check; ``subject`` is the path or entry name it
    concerns. Consumers key their own messages on ``reason``.
    """

    def __init__(self, reason: StableFsReason, subject: str | Path) -> None:
        super().__init__(f"{reason}: {subject}")
        self.reason: StableFsReason = reason
        self.subject = str(subject)


def directory_open_flags() -> int:
    return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def inode_identity(details: os.stat_result) -> tuple[int, int]:
    return int(details.st_dev), int(details.st_ino)


def directory_identity(details: os.stat_result, *, subject: str | Path = "") -> tuple[int, int]:
    """Return ``(st_dev, st_ino)`` for a directory ``stat`` result, refusing anything else."""

    if stat.S_ISLNK(details.st_mode):
        raise StableFsError("symlink", subject)
    if not stat.S_ISDIR(details.st_mode):
        raise StableFsError("not_directory", subject)
    return inode_identity(details)


def require_safe_basename(name: str) -> None:
    """Refuse an entry name that is empty, has separators, or names ``.``/``..``."""

    if not name or Path(name).name != name or name in {".", ".."}:
        raise StableFsError("unsafe_name", name)


def _lstat_identity_after_open(
    name: str | Path,
    *,
    dir_fd: int | None,
    subject: str | Path,
) -> tuple[int, int]:
    try:
        after = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise StableFsError("missing", subject) from None
    return directory_identity(after, subject=subject)


def _pin_opened_directory(
    descriptor: int,
    name: str | Path,
    *,
    dir_fd: int | None,
    before_identity: tuple[int, int],
    expected_identity: tuple[int, int] | None,
    subject: str | Path,
) -> tuple[int, tuple[int, int]]:
    try:
        opened_identity = directory_identity(os.fstat(descriptor), subject=subject)
        if expected_identity is not None and opened_identity != expected_identity:
            raise StableFsError("unexpected_identity", subject)
        after_identity = _lstat_identity_after_open(name, dir_fd=dir_fd, subject=subject)
        if not (before_identity == opened_identity == after_identity):
            raise StableFsError("identity_changed", subject)
        return descriptor, opened_identity
    except BaseException:
        os.close(descriptor)
        raise


def open_pinned_directory(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, tuple[int, int]]:
    """Open ``path`` as a directory without following a final symlink.

    A symlink at ``path`` raises ``StableFsError("symlink")`` before any open.
    A non-directory surfaces as the ``NotADirectoryError`` that ``O_DIRECTORY``
    raises, and a missing path as ``FileNotFoundError``.
    """

    before = os.stat(path, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode):
        raise StableFsError("symlink", path)
    descriptor = os.open(path, directory_open_flags())
    return _pin_opened_directory(
        descriptor,
        path,
        dir_fd=None,
        before_identity=inode_identity(before),
        expected_identity=expected_identity,
        subject=path,
    )


def open_pinned_directory_at(
    parent_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int] | None = None,
    missing_ok: bool = False,
) -> tuple[int, tuple[int, int]] | None:
    """Open the child ``name`` of ``parent_fd`` as a pinned directory.

    Returns ``None`` when the entry is absent and ``missing_ok`` is set; an
    absent entry otherwise raises ``StableFsError("missing")``.
    """

    require_safe_basename(name)
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise StableFsError("missing", name) from None
    if stat.S_ISLNK(before.st_mode):
        raise StableFsError("symlink", name)
    descriptor = os.open(name, directory_open_flags(), dir_fd=parent_fd)
    return _pin_opened_directory(
        descriptor,
        name,
        dir_fd=parent_fd,
        before_identity=inode_identity(before),
        expected_identity=expected_identity,
        subject=name,
    )


def require_directory_path_identity(
    path: Path,
    descriptor: int,
    expected_identity: tuple[int, int],
) -> None:
    """Re-check that ``descriptor`` and the pathname ``path`` still name ``expected_identity``."""

    descriptor_info = os.fstat(descriptor)
    try:
        path_info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise StableFsError("missing", path) from None
    if (
        not stat.S_ISDIR(path_info.st_mode)
        or inode_identity(descriptor_info) != expected_identity
        or inode_identity(path_info) != expected_identity
    ):
        raise StableFsError("identity_changed", path)


def read_stable_regular_file_at(
    name: str | Path,
    *,
    dir_fd: int | None = None,
    max_bytes: int,
    require_single_link: bool = False,
    chunk_bytes: int = DEFAULT_READ_CHUNK_BYTES,
) -> tuple[bytes, os.stat_result]:
    """Read one regular file without following a final symlink or blocking on a FIFO.

    The bytes are accepted only when the file's device, inode, size and mtime
    are unchanged across the read and the byte count matches the final size.
    Returns the payload and the post-read ``fstat`` result. ``OSError`` from the
    open propagates unchanged.
    """

    descriptor = open_pinned_readonly(name, dir_fd=dir_fd)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise StableFsError("not_regular", name)
        if require_single_link and before.st_nlink != 1:
            raise StableFsError("not_single_link", name)
        if before.st_size > max_bytes:
            raise StableFsError("too_large", name)
        payload = bytearray()
        while chunk := os.read(descriptor, min(chunk_bytes, max_bytes + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise StableFsError("too_large", name)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or len(payload) != after.st_size:
            raise StableFsError("changed", name)
        return bytes(payload), after
    finally:
        os.close(descriptor)


def unlink_at_if_present(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def atomic_write_bytes_at(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    mode: int,
    temp_prefix: str,
    force_mode: bool = True,
) -> None:
    """Publish ``payload`` under ``name`` in ``directory_fd`` through a fsynced temp file.

    ``force_mode`` applies ``mode`` exactly with ``fchmod`` (ignoring the umask);
    without it the created file keeps the umask-masked ``mode``.
    """

    require_safe_basename(name)
    temporary_name = f"{temp_prefix}{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary_name, flags, mode, dir_fd=directory_fd)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise StableFsError("short_write", name)
            view = view[written:]
        if force_mode:
            os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        unlink_at_if_present(directory_fd, temporary_name)


__all__ = [
    "DEFAULT_READ_CHUNK_BYTES",
    "StableFsError",
    "StableFsReason",
    "atomic_write_bytes_at",
    "directory_identity",
    "directory_open_flags",
    "inode_identity",
    "open_pinned_directory",
    "open_pinned_directory_at",
    "read_stable_regular_file_at",
    "require_directory_path_identity",
    "require_safe_basename",
    "unlink_at_if_present",
]
