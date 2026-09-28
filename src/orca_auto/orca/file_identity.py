"""Content identity of a pinned regular file: executables, inputs and outputs.

An identity is the resolved path, SHA-256 and byte size of a file read through
one pinned descriptor; the file must not change while it is hashed.
``open_pinned_executable`` keeps the descriptor open for the launch, and
``confined_output_identity`` also requires a single-link output inside its job
directory and syncs it before it answers.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from orca_auto.core.utils.persistence import fsync_directory, open_pinned_readonly


def _stable_key(details: os.stat_result) -> tuple[int, int, int, int]:
    return (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns)


def _hash_pinned(
    descriptor: int, before: os.stat_result, resolved: Path, *, label: str
) -> tuple[dict[str, Any], os.stat_result]:
    """Hash the whole pinned file and return its identity with its final status."""
    digest = hashlib.sha256()
    total_bytes = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
        total_bytes += len(chunk)
    after = os.fstat(descriptor)
    if _stable_key(before) != _stable_key(after) or total_bytes != after.st_size:
        raise ValueError(f"{label} changed while it was hashed: {resolved}")
    identity = {"path": str(resolved), "sha256": digest.hexdigest(), "size_bytes": total_bytes}
    return identity, after


def open_pinned_executable(
    path: str | Path, *, label: str = "Engine executable"
) -> tuple[int, dict[str, Any]]:
    """Pin a regular file by descriptor and return it, rewound, with its identity.

    The runner launches the pinned bytes directly; a caller that only needs the
    identity uses ``file_content_identity``.
    """
    resolved = Path(path).expanduser().resolve()
    descriptor = open_pinned_readonly(resolved)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} is not a regular file: {resolved}")
        identity, _after = _hash_pinned(descriptor, before, resolved, label=label)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def file_content_identity(path: str | Path, *, label: str = "File") -> dict[str, Any]:
    descriptor, identity = open_pinned_executable(path, label=label)
    os.close(descriptor)
    return identity


def verify_executable_identity(identity: Any) -> str:
    if not isinstance(identity, dict):
        raise ValueError("Queued execution snapshot has no executable identity")
    current = file_content_identity(str(identity.get("path") or ""), label="Engine executable")
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
        identity, after = _hash_pinned(descriptor, before, resolved, label="Engine output")
        os.fsync(descriptor)
        if _stable_key(os.fstat(descriptor)) != _stable_key(after):
            raise ValueError(f"Engine output changed while it was synchronized: {resolved}")
    finally:
        os.close(descriptor)
    fsync_directory(resolved.parent)
    return identity


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
    "file_content_identity",
    "open_pinned_executable",
    "verify_confined_output_identity",
    "verify_executable_identity",
]
