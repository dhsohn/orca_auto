from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from orca_auto.core.utils.persistence import fsync_directory, open_pinned_readonly


def open_pinned_executable(
    path: str | Path, *, label: str = "Engine executable"
) -> tuple[int, dict[str, Any]]:
    """Pin an executable by descriptor and return it with its content identity.

    The descriptor stays open and rewound so a caller can launch the pinned bytes
    directly; a caller that only needs the identity uses `executable_identity`.
    """
    resolved = Path(path).expanduser().resolve()
    descriptor = open_pinned_readonly(resolved)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} is not a regular file: {resolved}")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
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
        ):
            raise ValueError(f"{label} changed while it was hashed: {resolved}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, {
            "path": str(resolved),
            "sha256": digest.hexdigest(),
            "size_bytes": after.st_size,
        }
    except BaseException:
        os.close(descriptor)
        raise


def executable_identity(path: str | Path) -> dict[str, Any]:
    descriptor, identity = open_pinned_executable(path)
    try:
        return identity
    finally:
        os.close(descriptor)


def verify_executable_identity(identity: Any) -> str:
    if not isinstance(identity, dict):
        raise ValueError("Queued execution snapshot has no executable identity")
    current = executable_identity(str(identity.get("path") or ""))
    if current != identity:
        raise ValueError("Queued engine executable no longer matches its submitted identity")
    return str(current["path"])


def confined_output_identity(root: str | Path, path: str | Path) -> dict[str, Any]:
    resolved_root = Path(root).expanduser().resolve()
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"Engine output must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"Engine output must stay inside its job directory: {candidate}")
    descriptor = open_pinned_readonly(resolved)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError(f"Engine output must be a single-link regular file: {resolved}")
        digest = hashlib.sha256()
        total_bytes = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            total_bytes += len(chunk)
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
        ) or total_bytes != after.st_size:
            raise ValueError(f"Engine output changed while it was hashed: {resolved}")
        os.fsync(descriptor)
        synced = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            synced.st_dev,
            synced.st_ino,
            synced.st_size,
            synced.st_mtime_ns,
        ):
            raise ValueError(f"Engine output changed while it was synchronized: {resolved}")
    finally:
        os.close(descriptor)
    fsync_directory(resolved.parent)
    return {
        "path": str(resolved),
        "sha256": digest.hexdigest(),
        "size_bytes": total_bytes,
    }


def verify_confined_output_identity(
    root: str | Path,
    identity: Any,
    *,
    path_override: str | Path | None = None,
) -> Path:
    if not isinstance(identity, dict):
        raise ValueError("Engine output has no content identity")
    current = confined_output_identity(
        root,
        path_override if path_override is not None else str(identity.get("path") or ""),
    )
    if (
        current["sha256"] != identity.get("sha256")
        or current["size_bytes"] != identity.get("size_bytes")
        or (path_override is None and current["path"] != identity.get("path"))
    ):
        raise ValueError("Engine output no longer matches its terminal content identity")
    return Path(current["path"])


__all__ = [
    "confined_output_identity",
    "executable_identity",
    "open_pinned_executable",
    "verify_confined_output_identity",
    "verify_executable_identity",
]
