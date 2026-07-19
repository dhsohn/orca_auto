from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

JOB_PATH_IDENTITY_KEY = "job_path_identity"
_IDENTITY_VERSION = 1


def _absolute_path(value: str | Path, *, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"xTB-MD {label} must be an absolute path")
    if any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"xTB-MD {label} must be a canonical path")
    return path


def _directory_flags() -> int:
    flags = os.O_RDONLY
    flags |= os.O_CLOEXEC
    flags |= os.O_DIRECTORY
    flags |= os.O_NOFOLLOW
    return flags


def _descriptor(path: Path, status: os.stat_result) -> dict[str, Any]:
    if not stat.S_ISDIR(status.st_mode):
        raise ValueError(f"xTB-MD path component is not a directory: {path}")
    return {
        "path": str(path),
        "device": int(status.st_dev),
        "inode": int(status.st_ino),
        "uid": int(status.st_uid),
        "gid": int(status.st_gid),
    }


def _same_named_directory(
    path: Path,
    opened: os.stat_result,
    named: os.stat_result,
) -> None:
    if not stat.S_ISDIR(named.st_mode) or (
        int(opened.st_dev),
        int(opened.st_ino),
    ) != (
        int(named.st_dev),
        int(named.st_ino),
    ):
        raise ValueError(f"xTB-MD directory changed while its identity was inspected: {path}")


def _inspect_directory_chain(path: Path) -> list[dict[str, Any]]:
    flags = _directory_flags()
    descriptor = os.open("/", flags)
    current = Path("/")
    rows: list[dict[str, Any]] = []
    try:
        root_status = os.fstat(descriptor)
        root_named = os.lstat("/")
        _same_named_directory(current, root_status, root_named)
        rows.append(_descriptor(current, root_status))
        for component in path.parts[1:]:
            try:
                before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                child_descriptor = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise ValueError(
                    f"xTB-MD directory path is missing, replaced, or contains a symlink: {path}"
                ) from exc
            try:
                opened = os.fstat(child_descriptor)
                after = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                next_path = current / component
                _same_named_directory(next_path, opened, before)
                _same_named_directory(next_path, opened, after)
            except BaseException:
                os.close(child_descriptor)
                raise
            os.close(descriptor)
            descriptor = child_descriptor
            current = next_path
            rows.append(_descriptor(current, opened))

        final_named = path.lstat()
        _same_named_directory(path, os.fstat(descriptor), final_named)
        return rows
    finally:
        os.close(descriptor)


def _row_for_path(rows: list[dict[str, Any]], path: Path) -> dict[str, Any]:
    matches = [row for row in rows if row.get("path") == str(path)]
    if len(matches) != 1:
        raise ValueError(f"xTB-MD directory identity is missing path: {path}")
    return dict(matches[0])


def capture_job_path_identity(
    job_dir: str | Path,
    runs_root: str | Path,
) -> dict[str, Any]:
    canonical_job_dir = _absolute_path(job_dir, label="job directory")
    canonical_runs_root = _absolute_path(runs_root, label="runs root")
    if not canonical_job_dir.is_relative_to(canonical_runs_root):
        raise ValueError("xTB-MD job directory must stay inside runs_root")
    ancestry = _inspect_directory_chain(canonical_job_dir)
    runs_root_identity = _row_for_path(ancestry, canonical_runs_root)
    job_dir_identity = _row_for_path(ancestry, canonical_job_dir)
    return {
        "version": _IDENTITY_VERSION,
        "worker_uid": int(os.geteuid()),
        "runs_root": runs_root_identity,
        "job_dir": job_dir_identity,
        "ancestry": ancestry,
    }


def _identity_path(raw: object, *, label: str) -> Path:
    if not isinstance(raw, Mapping):
        raise ValueError(f"xTB-MD execution snapshot has no {label} identity")
    value = str(raw.get("path") or "").strip()
    if not value:
        raise ValueError(f"xTB-MD execution snapshot has no {label} path")
    return _absolute_path(value, label=label)


def validate_execution_snapshot_job_dir(
    configured_runs_root: str | Path,
    execution_snapshot: Mapping[str, Any],
) -> Path:
    raw_identity = execution_snapshot.get(JOB_PATH_IDENTITY_KEY)
    if not isinstance(raw_identity, Mapping):
        raise ValueError("xTB-MD execution snapshot has no bound job path identity")
    if raw_identity.get("version") != _IDENTITY_VERSION:
        raise ValueError("xTB-MD execution snapshot has an unsupported job path identity")
    if raw_identity.get("worker_uid") != os.geteuid():
        raise ValueError("xTB-MD execution worker identity changed after submission")

    expected_runs_root = _identity_path(raw_identity.get("runs_root"), label="runs root")
    expected_job_dir = _identity_path(raw_identity.get("job_dir"), label="job directory")
    current_runs_root = _absolute_path(configured_runs_root, label="configured runs root")
    if current_runs_root != expected_runs_root:
        raise ValueError("xTB-MD configured runs_root changed after submission")
    if not expected_job_dir.is_relative_to(expected_runs_root):
        raise ValueError("xTB-MD bound job directory is outside runs_root")
    if str(execution_snapshot.get("job_dir") or "") != str(expected_job_dir):
        raise ValueError("xTB-MD execution snapshot job directory changed")

    expected_ancestry = raw_identity.get("ancestry")
    if not isinstance(expected_ancestry, list) or not all(
        isinstance(row, Mapping) for row in expected_ancestry
    ):
        raise ValueError("xTB-MD execution snapshot has an invalid directory ancestry")
    current_ancestry = _inspect_directory_chain(expected_job_dir)
    if current_ancestry != [dict(row) for row in expected_ancestry]:
        raise ValueError("xTB-MD job directory ancestry changed after submission")
    return expected_job_dir


__all__ = [
    "JOB_PATH_IDENTITY_KEY",
    "capture_job_path_identity",
    "validate_execution_snapshot_job_dir",
]
