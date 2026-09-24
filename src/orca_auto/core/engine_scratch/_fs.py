"""Descriptor-pinned filesystem primitives with scratch-specific error text:
thin shims over ``core.utils.stable_fs`` that translate ``StableFsError``
into ``EngineScratchError`` (pinned directory opens, path identity checks,
atomic staging writes, bounded stable reads) plus the fd-relative hashing
and existence helpers the staging, publication and inspection code share.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from orca_auto.core.queue.engine.input_snapshot import MAX_INPUT_SNAPSHOT_BYTES
from orca_auto.core.utils import stable_fs
from orca_auto.core.utils.persistence import open_pinned_readonly

from ._constants import (
    _COPY_CHUNK_BYTES,
    _STAGING_TEMP_PREFIX,
)
from ._errors import EngineScratchError


def _open_pinned_directory_at(
    parent_fd: int,
    name: str,
    *,
    display_path: Path,
    label: str,
) -> tuple[int, tuple[int, int]]:
    try:
        opened = stable_fs.open_pinned_directory_at(parent_fd, name)
    except stable_fs.StableFsError as exc:
        if exc.reason == "unsafe_name":
            raise EngineScratchError(f"{label} has an unsafe basename: {name!r}") from exc
        if exc.reason == "missing":
            raise EngineScratchError(f"{label} pathname disappeared: {display_path}") from exc
        if exc.reason == "symlink":
            raise EngineScratchError(f"{label} is a symlink: {display_path}") from exc
        if exc.reason == "not_directory":
            raise EngineScratchError(f"{label} is not a directory: {display_path}") from exc
        raise EngineScratchError(f"{label} identity changed: {display_path}") from exc
    assert opened is not None
    return opened


def _open_pinned_directory(
    path: Path,
    *,
    label: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, tuple[int, int]]:
    try:
        return stable_fs.open_pinned_directory(path, expected_identity=expected_identity)
    except stable_fs.StableFsError as exc:
        if exc.reason == "symlink":
            raise EngineScratchError(f"{label} is a symlink: {path}") from exc
        if exc.reason == "not_directory":
            raise EngineScratchError(f"{label} is not a directory: {path}") from exc
        if exc.reason == "unexpected_identity":
            raise EngineScratchError(
                f"{label} identity changed before scratch launch: {path}"
            ) from exc
        raise _path_identity_error(exc, label=label, path=path) from exc


def _require_directory_path_identity(
    path: Path,
    descriptor: int,
    expected_identity: tuple[int, int],
    *,
    label: str,
) -> None:
    try:
        stable_fs.require_directory_path_identity(path, descriptor, expected_identity)
    except stable_fs.StableFsError as exc:
        raise _path_identity_error(exc, label=label, path=path) from exc


def _path_identity_error(
    exc: stable_fs.StableFsError,
    *,
    label: str,
    path: Path,
) -> EngineScratchError:
    if exc.reason == "missing":
        return EngineScratchError(f"{label} pathname disappeared: {path}")
    return EngineScratchError(f"{label} pathname identity changed: {path}")


def _atomic_write_bytes_at(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    mode: int,
) -> None:
    try:
        stable_fs.atomic_write_bytes_at(
            directory_fd,
            name,
            payload,
            mode=mode,
            temp_prefix=_STAGING_TEMP_PREFIX,
        )
    except stable_fs.StableFsError as exc:
        if exc.reason == "unsafe_name":
            raise EngineScratchError(f"engine scratch staging name is unsafe: {name!r}") from exc
        raise EngineScratchError(f"Failed to stage engine scratch input: {name}") from exc


def _read_stable_regular_file_at(
    directory_fd: int,
    name: str,
    *,
    display_path: Path,
    max_bytes: int = MAX_INPUT_SNAPSHOT_BYTES,
) -> tuple[bytes, int]:
    try:
        payload, details = stable_fs.read_stable_regular_file_at(
            name,
            dir_fd=directory_fd,
            max_bytes=max_bytes,
            require_single_link=True,
            chunk_bytes=_COPY_CHUNK_BYTES,
        )
    except stable_fs.StableFsError as exc:
        if exc.reason == "too_large":
            raise EngineScratchError(
                f"engine input exceeds the scratch staging limit ({max_bytes} bytes): "
                f"{display_path}"
            ) from exc
        if exc.reason == "changed":
            raise EngineScratchError(f"engine input changed while staged: {display_path}") from exc
        raise EngineScratchError(
            f"engine input is not a private regular file: {display_path}"
        ) from exc
    return payload, stat.S_IMODE(details.st_mode)


def _regular_file_sha256_at(
    directory_fd: int,
    name: str,
    *,
    display_path: Path,
) -> tuple[str, int]:
    descriptor = open_pinned_readonly(name, dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise EngineScratchError(f"engine durable artifact is unsafe: {display_path}")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, _COPY_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
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
        ) or size != after.st_size:
            raise EngineScratchError(f"engine durable artifact changed while read: {display_path}")
        return digest.hexdigest(), size
    finally:
        os.close(descriptor)


def _entry_exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True
