from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from orca_auto.core.utils.persistence import open_pinned_readonly

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
                flags=os.XATTR_CREATE,
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


def _remove_emptied_directory_at(
    parent_fd: int,
    name: str,
    *,
    child_fd: int,
    child_identity: tuple[int, int],
    label: str,
) -> None:
    """Empty a pinned directory, then unlink it only while it is still that directory.

    The name is reopened and matched against the identity that was pinned before
    the contents were removed, so a directory swapped in during the removal is
    refused instead of being unlinked in the original's place.
    """

    _remove_directory_contents_at(child_fd, label=label)
    verified_child = _open_stable_directory_at(
        parent_fd,
        name,
        label=label,
        expected_identity=child_identity,
    )
    assert verified_child is not None
    verified_child_fd, _identity = verified_child
    try:
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(verified_child_fd)
    os.fsync(parent_fd)


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
            _remove_emptied_directory_at(
                job_fd,
                safe_namespace,
                child_fd=generation_fd,
                child_identity=generation_identity,
                label=f"{label} generation",
            )
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
    try:
        descriptor = open_pinned_readonly(source_path)
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


__all__ = [
    "MAX_INPUT_SNAPSHOT_BYTES",
    "canonical_input_snapshot_namespace",
    "cleanup_unowned_direct_generation_directory",
    "read_stable_regular_file",
]
