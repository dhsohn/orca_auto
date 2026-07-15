from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.utils.persistence import durable_mkdir, fsync_directory

SNAPSHOT_DIR_NAME = ".orca_auto_input_snapshots"
MAX_INPUT_SNAPSHOT_BYTES = 64 * 1024 * 1024
_ROLE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DIRECT_GENERATION_OWNER_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]{16,160}")
_DIRECT_GENERATION_OWNER_XATTR = "user.orca_auto.generation_owner"


def _safe_role(role: str) -> str:
    value = _ROLE_RE.sub("_", str(role).strip()).strip("._-")
    if not value:
        raise ValueError("Input snapshot role must not be empty")
    return value[:80]


def canonical_input_snapshot_namespace(namespace: str) -> str:
    """Return an exact safe namespace, rejecting lossy normalization or truncation."""

    original = str(namespace)
    raw = original.strip()
    canonical = _safe_role(raw)
    if original != raw or canonical != original:
        raise ValueError(
            "Input snapshot namespace must already be a safe path segment of at most 80 characters"
        )
    return canonical


def _directory_open_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("Safe no-follow directory access is unavailable")
    return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def _directory_identity(details: os.stat_result) -> tuple[int, int]:
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError("Snapshot cleanup path must be a directory")
    return int(details.st_dev), int(details.st_ino)


def _open_stable_directory(
    path: Path,
    *,
    label: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, tuple[int, int]]:
    try:
        before = os.stat(path, follow_symlinks=False)
        before_identity = _directory_identity(before)
        descriptor = os.open(path, _directory_open_flags())
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} is unavailable or unsafe") from exc
    try:
        opened_identity = _directory_identity(os.fstat(descriptor))
        after_identity = _directory_identity(os.stat(path, follow_symlinks=False))
        if not (
            before_identity == opened_identity == after_identity
            and (expected_identity is None or opened_identity == expected_identity)
        ):
            raise ValueError(f"{label} identity changed")
        return descriptor, opened_identity
    except BaseException:
        os.close(descriptor)
        raise


def _open_stable_directory_at(
    parent_fd: int,
    name: str,
    *,
    label: str,
    missing_ok: bool = False,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, tuple[int, int]] | None:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ValueError(f"{label} identity changed") from None
    try:
        before_identity = _directory_identity(before)
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} is unavailable or unsafe") from exc
    try:
        opened_identity = _directory_identity(os.fstat(descriptor))
        after_identity = _directory_identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
        if not (
            before_identity == opened_identity == after_identity
            and (expected_identity is None or opened_identity == expected_identity)
        ):
            raise ValueError(f"{label} identity changed")
        return descriptor, opened_identity
    except BaseException:
        os.close(descriptor)
        raise


def _direct_generation_owner_payload(token: str) -> bytes:
    normalized = str(token).strip()
    if not _DIRECT_GENERATION_OWNER_TOKEN_RE.fullmatch(normalized):
        raise ValueError("Generation owner token contains unsupported characters")
    return f"orca_auto-visible-generation-owner-v1:{normalized}".encode()


def _verify_direct_generation_owner(directory_fd: int, token: str) -> None:
    expected = _direct_generation_owner_payload(token)
    try:
        current = os.getxattr(directory_fd, _DIRECT_GENERATION_OWNER_XATTR)
    except OSError as exc:
        raise ValueError("Visible generation owner identity is unavailable") from exc
    if current != expected:
        raise ValueError("Visible generation owner identity changed")


def bind_direct_generation_owner(
    job_dir: str | Path,
    *,
    namespace: str,
    expected_job_identity: tuple[int, int],
    expected_generation_identity: tuple[int, int],
    owner_token: str,
) -> None:
    """Bind an invisible durable owner token to one newly-created generation inode."""

    safe_namespace = canonical_input_snapshot_namespace(namespace)
    resolved_job_dir = Path(job_dir).expanduser().resolve(strict=True)
    job_fd, _identity = _open_stable_directory(
        resolved_job_dir,
        label="Generation owner job directory",
        expected_identity=expected_job_identity,
    )
    try:
        opened = _open_stable_directory_at(
            job_fd,
            safe_namespace,
            label="Generation owner directory",
            expected_identity=expected_generation_identity,
        )
        assert opened is not None
        generation_fd, _generation_identity = opened
        try:
            os.setxattr(
                generation_fd,
                _DIRECT_GENERATION_OWNER_XATTR,
                _direct_generation_owner_payload(owner_token),
                flags=getattr(os, "XATTR_CREATE", 1),
            )
            os.fsync(generation_fd)
            _verify_direct_generation_owner(generation_fd, owner_token)
        finally:
            os.close(generation_fd)
    finally:
        os.close(job_fd)


def require_direct_generation_owner(
    job_dir: str | Path,
    *,
    namespace: str,
    expected_job_identity: tuple[int, int],
    expected_generation_identity: tuple[int, int],
    owner_token: str,
) -> None:
    """Require one direct generation to retain its durable invisible owner token."""

    safe_namespace = canonical_input_snapshot_namespace(namespace)
    resolved_job_dir = Path(job_dir).expanduser().resolve(strict=True)
    job_fd, _identity = _open_stable_directory(
        resolved_job_dir,
        label="Generation owner job directory",
        expected_identity=expected_job_identity,
    )
    try:
        opened = _open_stable_directory_at(
            job_fd,
            safe_namespace,
            label="Generation owner directory",
            expected_identity=expected_generation_identity,
        )
        assert opened is not None
        generation_fd, _generation_identity = opened
        try:
            _verify_direct_generation_owner(generation_fd, owner_token)
        finally:
            os.close(generation_fd)
    finally:
        os.close(job_fd)


def _remove_directory_contents_at(directory_fd: int, *, label: str) -> None:
    """Remove entries below one pinned directory without following links."""

    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        try:
            details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(details.st_mode):
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            continue

        opened = _open_stable_directory_at(
            directory_fd,
            name,
            label=f"{label} component",
        )
        assert opened is not None
        child_fd, child_identity = opened
        try:
            _remove_directory_contents_at(child_fd, label=label)
            verified = _open_stable_directory_at(
                directory_fd,
                name,
                label=f"{label} component",
                expected_identity=child_identity,
            )
            assert verified is not None
            verified_fd, _identity = verified
            try:
                os.rmdir(name, dir_fd=directory_fd)
            finally:
                os.close(verified_fd)
        finally:
            os.close(child_fd)
    os.fsync(directory_fd)


def _cleanup_unowned_generation_directory(
    job_dir: str | Path,
    *,
    root_name: str,
    namespace: str,
    label: str,
    expected_job_identity: tuple[int, int] | None = None,
    remove_root_if_empty: bool = True,
) -> None:
    """Remove one unowned generation through pinned, no-follow directory descriptors."""

    if (
        not root_name
        or root_name in {".", ".."}
        or "\x00" in root_name
        or Path(root_name).name != root_name
    ):
        raise ValueError(f"{label} root must be one safe path component")
    safe_namespace = canonical_input_snapshot_namespace(namespace)
    raw_job_dir = Path(job_dir).expanduser()
    try:
        resolved_job_dir = raw_job_dir.resolve(strict=True)
    except FileNotFoundError:
        if expected_job_identity is not None:
            raise ValueError("Snapshot cleanup job directory identity changed") from None
        return
    job_fd, _job_identity = _open_stable_directory(
        resolved_job_dir,
        label="Snapshot cleanup job directory",
        expected_identity=expected_job_identity,
    )
    try:
        opened_root = _open_stable_directory_at(
            job_fd,
            root_name,
            label=f"{label} root",
            missing_ok=True,
        )
        if opened_root is None:
            return
        root_fd, root_identity = opened_root
        try:
            opened_namespace = _open_stable_directory_at(
                root_fd,
                safe_namespace,
                label=f"{label} generation",
                missing_ok=True,
            )
            if opened_namespace is None:
                return
            namespace_fd, namespace_identity = opened_namespace
            try:
                _remove_directory_contents_at(namespace_fd, label=f"{label} generation")
                verified_namespace = _open_stable_directory_at(
                    root_fd,
                    safe_namespace,
                    label=f"{label} generation",
                    expected_identity=namespace_identity,
                )
                assert verified_namespace is not None
                verified_namespace_fd, _identity = verified_namespace
                try:
                    os.rmdir(safe_namespace, dir_fd=root_fd)
                finally:
                    os.close(verified_namespace_fd)
                os.fsync(root_fd)
            finally:
                os.close(namespace_fd)

            if not remove_root_if_empty:
                return
            verified_root = _open_stable_directory_at(
                job_fd,
                root_name,
                label=f"{label} root",
                expected_identity=root_identity,
            )
            assert verified_root is not None
            verified_root_fd, _identity = verified_root
            try:
                try:
                    os.rmdir(root_name, dir_fd=job_fd)
                except OSError as exc:
                    if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                        raise
                    os.fsync(root_fd)
                else:
                    os.fsync(job_fd)
            finally:
                os.close(verified_root_fd)
        finally:
            os.close(root_fd)
    finally:
        os.close(job_fd)


def cleanup_unowned_direct_generation_directory(
    job_dir: str | Path,
    *,
    namespace: str,
    label: str,
    expected_job_identity: tuple[int, int],
    expected_generation_identity: tuple[int, int],
    expected_owner_token: str,
) -> None:
    """Remove one exact direct child generation through pinned no-follow handles."""

    safe_namespace = canonical_input_snapshot_namespace(namespace)
    raw_job_dir = Path(job_dir).expanduser()
    try:
        resolved_job_dir = raw_job_dir.resolve(strict=True)
    except FileNotFoundError:
        raise ValueError("Generation cleanup job directory identity changed") from None
    job_fd, _job_identity = _open_stable_directory(
        resolved_job_dir,
        label="Generation cleanup job directory",
        expected_identity=expected_job_identity,
    )
    try:
        opened_generation = _open_stable_directory_at(
            job_fd,
            safe_namespace,
            label=f"{label} generation",
            missing_ok=True,
            expected_identity=expected_generation_identity,
        )
        if opened_generation is None:
            return
        generation_fd, generation_identity = opened_generation
        try:
            _verify_direct_generation_owner(generation_fd, expected_owner_token)
            _remove_directory_contents_at(generation_fd, label=f"{label} generation")
            verified_generation = _open_stable_directory_at(
                job_fd,
                safe_namespace,
                label=f"{label} generation",
                expected_identity=generation_identity,
            )
            assert verified_generation is not None
            verified_generation_fd, _identity = verified_generation
            try:
                os.rmdir(safe_namespace, dir_fd=job_fd)
            finally:
                os.close(verified_generation_fd)
            os.fsync(job_fd)
        finally:
            os.close(generation_fd)
    finally:
        os.close(job_fd)


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
    namespaced_root = snapshot_root / canonical_input_snapshot_namespace(namespace)
    if namespaced_root.is_symlink():
        raise ValueError(f"Input snapshot namespace must not be a symlink: {namespaced_root}")
    durable_mkdir(namespaced_root, mode=0o700, exist_ok=True)
    return namespaced_root


def reserve_input_snapshot_namespace(job_dir: str | Path, namespace: str) -> Path:
    """Exclusively reserve one submission generation namespace."""

    resolved_job_dir = Path(job_dir).expanduser().resolve()
    snapshot_root = _ensure_snapshot_root(resolved_job_dir)
    namespace_dir = snapshot_root / canonical_input_snapshot_namespace(namespace)
    if namespace_dir.is_symlink():
        raise ValueError(f"Input snapshot namespace must not be a symlink: {namespace_dir}")
    created = False
    try:
        namespace_dir.mkdir(mode=0o700, exist_ok=False)
        created = True
        fsync_directory(snapshot_root)
    except BaseException:
        if created:
            _cleanup_unowned_generation_directory(
                resolved_job_dir,
                root_name=SNAPSHOT_DIR_NAME,
                namespace=namespace,
                label="Input snapshot",
                remove_root_if_empty=False,
            )
        raise
    return namespace_dir.resolve()


def input_snapshot_namespace_dir(job_dir: str | Path, namespace: str) -> Path:
    """Return one existing, confined snapshot generation namespace."""

    resolved_job_dir = Path(job_dir).expanduser().resolve()
    snapshot_root = _ensure_snapshot_root(resolved_job_dir)
    namespace_dir = snapshot_root / canonical_input_snapshot_namespace(namespace)
    if namespace_dir.is_symlink():
        raise ValueError(f"Input snapshot namespace must not be a symlink: {namespace_dir}")
    resolved_namespace = namespace_dir.resolve()
    if not namespace_dir.is_dir() or resolved_namespace.parent != snapshot_root.resolve():
        raise ValueError(f"Input snapshot namespace is not a confined directory: {namespace_dir}")
    return resolved_namespace


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
    *,
    expected_job_identity: tuple[int, int] | None = None,
) -> None:
    """Remove one generation namespace that never obtained a durable queue owner."""

    _cleanup_unowned_generation_directory(
        job_dir,
        root_name=SNAPSHOT_DIR_NAME,
        namespace=namespace,
        label="Input snapshot",
        expected_job_identity=expected_job_identity,
    )


__all__ = [
    "SNAPSHOT_DIR_NAME",
    "MAX_INPUT_SNAPSHOT_BYTES",
    "canonical_input_snapshot_namespace",
    "cleanup_unowned_direct_generation_directory",
    "cleanup_unowned_input_snapshot_namespace",
    "input_snapshot_namespace_dir",
    "read_stable_regular_file",
    "reserve_input_snapshot_namespace",
    "snapshot_input_file",
    "snapshot_input_payload",
    "verify_input_snapshot",
    "verify_input_snapshots",
]
