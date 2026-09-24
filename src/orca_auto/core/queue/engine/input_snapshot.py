from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from orca_auto.core.utils import stable_fs
from orca_auto.core.utils.stable_fs import StableFsError

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


def _stable_directory_error(exc: StableFsError, *, label: str) -> ValueError:
    if exc.reason in {"unsafe_name", "symlink", "not_directory"}:
        return ValueError(f"{label} is unavailable or unsafe")
    return ValueError(f"{label} identity changed")


def _open_stable_directory(
    path: Path,
    *,
    label: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, tuple[int, int]]:
    try:
        return stable_fs.open_pinned_directory(path, expected_identity=expected_identity)
    except StableFsError as exc:
        raise _stable_directory_error(exc, label=label) from exc
    except OSError as exc:
        raise ValueError(f"{label} is unavailable or unsafe") from exc


def _open_stable_directory_at(
    parent_fd: int,
    name: str,
    *,
    label: str,
    missing_ok: bool = False,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, tuple[int, int]] | None:
    try:
        return stable_fs.open_pinned_directory_at(
            parent_fd,
            name,
            expected_identity=expected_identity,
            missing_ok=missing_ok,
        )
    except StableFsError as exc:
        raise _stable_directory_error(exc, label=label) from exc
    except OSError as exc:
        raise ValueError(f"{label} is unavailable or unsafe") from exc


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
        payload, _details = stable_fs.read_stable_regular_file_at(
            source_path,
            max_bytes=effective_max_bytes,
            require_single_link=require_single_link,
        )
    except StableFsError as exc:
        if exc.reason == "not_regular":
            raise ValueError(f"Input source is not a regular file: {source_path}") from exc
        if exc.reason == "not_single_link":
            raise ValueError(
                f"Input source must be a single-link regular file: {source_path}"
            ) from exc
        if exc.reason == "too_large":
            raise ValueError(
                f"Input source exceeds {effective_max_bytes} bytes: {source_path}"
            ) from exc
        raise ValueError(f"Input source changed while it was read: {source_path}") from exc
    except OSError as exc:
        raise ValueError(f"Input source is not a readable regular file: {source_path}") from exc
    return payload


__all__ = [
    "MAX_INPUT_SNAPSHOT_BYTES",
    "canonical_input_snapshot_namespace",
    "cleanup_unowned_direct_generation_directory",
    "read_stable_regular_file",
]
