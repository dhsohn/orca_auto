from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.utils.persistence import durable_mkdir, fsync_directory

SNAPSHOT_DIR_NAME = ".orca_auto_input_snapshots"
MAX_INPUT_SNAPSHOT_BYTES = 64 * 1024 * 1024
_ROLE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_role(role: str) -> str:
    value = _ROLE_RE.sub("_", str(role).strip()).strip("._-")
    if not value:
        raise ValueError("Input snapshot role must not be empty")
    return value[:80]


def read_stable_regular_file(
    path: str | Path,
    *,
    max_bytes: int = MAX_INPUT_SNAPSHOT_BYTES,
    require_single_link: bool = False,
) -> bytes:
    """Read one regular file without following a final symlink or blocking on a FIFO."""

    source_path = Path(path).expanduser()
    if max_bytes < 1:
        raise ValueError("Stable file read limit must be positive")
    effective_max_bytes = int(max_bytes)
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source_path, flags)
    except OSError as exc:
        raise ValueError(f"Input source is not a readable regular file: {source_path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Input source is not a regular file: {source_path}")
        if require_single_link and before.st_nlink != 1:
            raise ValueError(f"Input source must be a single-link regular file: {source_path}")
        if before.st_size > effective_max_bytes:
            raise ValueError(f"Input source exceeds {effective_max_bytes} bytes: {source_path}")
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            remaining = effective_max_bytes - total_bytes
            chunk = os.read(descriptor, min(1024 * 1024, remaining + 1))
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > effective_max_bytes:
                raise ValueError(f"Input source exceeds {effective_max_bytes} bytes: {source_path}")
            chunks.append(chunk)
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
            raise ValueError(f"Input source changed while it was read: {source_path}")
        payload = b"".join(chunks)
        if len(payload) != after.st_size:
            raise ValueError(f"Input source changed while it was read: {source_path}")
        return payload
    finally:
        os.close(descriptor)


def _ensure_snapshot_root(resolved_job_dir: Path) -> Path:
    snapshot_root = resolved_job_dir / SNAPSHOT_DIR_NAME
    if snapshot_root.is_symlink():
        raise ValueError(f"Input snapshot directory must not be a symlink: {snapshot_root}")
    durable_mkdir(snapshot_root, mode=0o700, parents=True, exist_ok=True)
    if not snapshot_root.is_dir() or not snapshot_root.resolve().is_relative_to(resolved_job_dir):
        raise ValueError(
            f"Input snapshot directory must stay inside the job directory: {snapshot_root}"
        )
    root_status = snapshot_root.stat()
    if root_status.st_uid != os.geteuid():
        raise ValueError(
            f"Input snapshot directory must be owned by the current user: {snapshot_root}"
        )
    if stat.S_IMODE(root_status.st_mode) & 0o022:
        os.chmod(snapshot_root, 0o700)
        root_status = snapshot_root.stat()
        if stat.S_IMODE(root_status.st_mode) & 0o022:
            raise ValueError(
                f"Input snapshot directory must not be group/world writable: {snapshot_root}"
            )
    return snapshot_root


def _snapshot_namespace(snapshot_root: Path, namespace: str | None) -> Path:
    if namespace is None:
        return snapshot_root
    namespaced_root = snapshot_root / _safe_role(namespace)
    if namespaced_root.is_symlink():
        raise ValueError(f"Input snapshot namespace must not be a symlink: {namespaced_root}")
    durable_mkdir(namespaced_root, mode=0o700, exist_ok=True)
    return namespaced_root


def reserve_input_snapshot_namespace(job_dir: str | Path, namespace: str) -> Path:
    """Exclusively reserve one submission generation namespace."""

    resolved_job_dir = Path(job_dir).expanduser().resolve()
    snapshot_root = _ensure_snapshot_root(resolved_job_dir)
    namespace_dir = snapshot_root / _safe_role(namespace)
    if namespace_dir.is_symlink():
        raise ValueError(f"Input snapshot namespace must not be a symlink: {namespace_dir}")
    created = False
    try:
        namespace_dir.mkdir(mode=0o700, exist_ok=False)
        created = True
        fsync_directory(snapshot_root)
    except BaseException:
        if created and namespace_dir.is_dir() and not namespace_dir.is_symlink():
            shutil.rmtree(namespace_dir, ignore_errors=True)
            fsync_directory(snapshot_root)
        raise
    return namespace_dir.resolve()


def snapshot_input_file(
    job_dir: str | Path,
    source: str | Path,
    *,
    role: str,
    max_bytes: int = MAX_INPUT_SNAPSHOT_BYTES,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Copy one confined input into a content-addressed submission snapshot."""

    resolved_job_dir = Path(job_dir).expanduser().resolve()
    resolved_source = Path(source).expanduser().resolve()
    if not resolved_source.is_relative_to(resolved_job_dir):
        raise ValueError(f"Input snapshot source must be inside the job directory: {source}")
    payload = read_stable_regular_file(
        resolved_source,
        max_bytes=min(int(max_bytes), MAX_INPUT_SNAPSHOT_BYTES),
    )
    digest = hashlib.sha256(payload).hexdigest()
    safe_role = _safe_role(role)
    suffix = resolved_source.suffix.lower()
    snapshot_root = _ensure_snapshot_root(resolved_job_dir)
    snapshot_root = _snapshot_namespace(snapshot_root, namespace)
    snapshot_path = snapshot_root / f"input-{digest}{suffix}"

    if snapshot_path.exists():
        existing = read_stable_regular_file(snapshot_path, require_single_link=True)
        if hashlib.sha256(existing).hexdigest() != digest or existing != payload:
            raise ValueError(f"Input snapshot digest collision or corruption: {snapshot_path}")
    else:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{safe_role}-",
            suffix=".tmp",
            dir=snapshot_root,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fchmod(handle.fileno(), 0o400)
                os.fsync(handle.fileno())
            os.replace(temporary_path, snapshot_path)
            fsync_directory(snapshot_root)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    return {
        "role": safe_role,
        "source_path": str(resolved_source),
        "snapshot_path": str(snapshot_path.resolve()),
        "sha256": digest,
        "size_bytes": len(payload),
    }


def snapshot_input_payload(
    job_dir: str | Path,
    payload: bytes,
    *,
    role: str,
    suffix: str,
    source_path: str | Path,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Persist generated submission bytes as a content-addressed input snapshot."""

    resolved_job_dir = Path(job_dir).expanduser().resolve()
    if len(payload) > MAX_INPUT_SNAPSHOT_BYTES:
        raise ValueError(f"Generated input snapshot exceeds {MAX_INPUT_SNAPSHOT_BYTES} bytes")
    resolved_source = Path(source_path).expanduser().resolve()
    if not resolved_source.is_relative_to(resolved_job_dir):
        raise ValueError(f"Input snapshot source must be inside the job directory: {source_path}")
    safe_role = _safe_role(role)
    safe_suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix) else ".bin"
    digest = hashlib.sha256(payload).hexdigest()
    snapshot_root = _ensure_snapshot_root(resolved_job_dir)
    snapshot_root = _snapshot_namespace(snapshot_root, namespace)
    snapshot_path = snapshot_root / f"{safe_role}-{digest}{safe_suffix}"
    if snapshot_path.exists():
        existing = read_stable_regular_file(snapshot_path, require_single_link=True)
        if existing != payload:
            raise ValueError(f"Input snapshot digest collision or corruption: {snapshot_path}")
    else:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{safe_role}-",
            suffix=".tmp",
            dir=snapshot_root,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fchmod(handle.fileno(), 0o400)
                os.fsync(handle.fileno())
            os.replace(temporary_path, snapshot_path)
            fsync_directory(snapshot_root)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return {
        "role": safe_role,
        "source_path": str(resolved_source),
        "snapshot_path": str(snapshot_path.resolve()),
        "sha256": digest,
        "size_bytes": len(payload),
    }


def verify_input_snapshot(
    job_dir: str | Path,
    descriptor: Mapping[str, Any],
    *,
    role: str,
) -> Path:
    """Validate a queued snapshot descriptor and return its exact staged path."""

    resolved_job_dir = Path(job_dir).expanduser().resolve()
    raw_snapshot_root = _ensure_snapshot_root(resolved_job_dir)
    expected_root = raw_snapshot_root.resolve()
    if not expected_root.is_relative_to(resolved_job_dir):
        raise ValueError("Input snapshot directory is outside the job directory")
    snapshot_text = str(descriptor.get("snapshot_path") or "").strip()
    if not snapshot_text:
        raise ValueError(f"Input snapshot {role!r} is missing snapshot_path")
    snapshot_path = Path(snapshot_text).expanduser().resolve()
    if not snapshot_path.is_relative_to(expected_root):
        raise ValueError(f"Input snapshot {role!r} is outside the snapshot directory")
    expected_role = _safe_role(role)
    if str(descriptor.get("role") or "").strip() != expected_role:
        raise ValueError(f"Input snapshot {role!r} has a mismatched role")
    source_text = str(descriptor.get("source_path") or "").strip()
    if not source_text or not Path(source_text).expanduser().resolve().is_relative_to(
        resolved_job_dir
    ):
        raise ValueError(f"Input snapshot {role!r} has an invalid source path")
    expected_digest = str(descriptor.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ValueError(f"Input snapshot {role!r} has an invalid SHA-256 digest")
    payload = read_stable_regular_file(snapshot_path, require_single_link=True)
    if len(payload) != descriptor.get("size_bytes"):
        raise ValueError(f"Input snapshot {role!r} has a mismatched size")
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        raise ValueError(f"Input snapshot {role!r} failed digest verification")
    return snapshot_path


def verify_input_snapshots(
    job_dir: str | Path,
    descriptors: Mapping[str, Any],
) -> dict[str, Path]:
    verified: dict[str, Path] = {}
    for role, raw_descriptor in descriptors.items():
        if not isinstance(role, str) or not isinstance(raw_descriptor, Mapping):
            raise ValueError("Input snapshot metadata must map roles to descriptor objects")
        verified[role] = verify_input_snapshot(
            job_dir,
            raw_descriptor,
            role=role,
        )
    if not verified:
        raise ValueError("Queued execution snapshot has no input files")
    return verified


def cleanup_unowned_input_snapshot_namespace(
    job_dir: str | Path,
    namespace: str,
) -> None:
    """Remove one generation namespace that never obtained a durable queue owner."""

    resolved_job_dir = Path(job_dir).expanduser().resolve()
    snapshot_root = resolved_job_dir / SNAPSHOT_DIR_NAME
    if snapshot_root.is_symlink():
        raise ValueError(f"Input snapshot directory must not be a symlink: {snapshot_root}")
    if not snapshot_root.exists():
        return
    resolved_root = snapshot_root.resolve()
    if not resolved_root.is_dir() or not resolved_root.is_relative_to(resolved_job_dir):
        raise ValueError("Input snapshot directory escapes its job directory")
    namespace_dir = snapshot_root / _safe_role(namespace)
    if namespace_dir.is_symlink():
        raise ValueError(f"Input snapshot namespace must not be a symlink: {namespace_dir}")
    if not namespace_dir.exists():
        return
    resolved_namespace = namespace_dir.resolve()
    if not resolved_namespace.is_dir() or resolved_namespace.parent != resolved_root:
        raise ValueError("Input snapshot namespace escapes its snapshot root")
    shutil.rmtree(resolved_namespace)
    fsync_directory(resolved_root)


__all__ = [
    "SNAPSHOT_DIR_NAME",
    "MAX_INPUT_SNAPSHOT_BYTES",
    "cleanup_unowned_input_snapshot_namespace",
    "read_stable_regular_file",
    "reserve_input_snapshot_namespace",
    "snapshot_input_file",
    "snapshot_input_payload",
    "verify_input_snapshot",
    "verify_input_snapshots",
]
