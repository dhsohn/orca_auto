from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..utils.lock import tmpfs_file_lock
from ..utils.persistence import atomic_write_json, coerce_int, resolve_root_path
from .location import JobLocationRecord
from .text import normalize_index_text as _normalize_text

JOB_LOCATION_INDEX_FILE_NAME = "job_locations.json"
JOB_LOCATION_INDEX_LOCK_NAME = "job_locations.lock"


class JobLocationIndexError(RuntimeError):
    """Raised when the job location index cannot satisfy a lookup."""


class JobLocationIndexCorruptError(JobLocationIndexError):
    """Raised when the job location index exists but cannot be safely loaded."""


def _index_path(root: Path) -> Path:
    return root / JOB_LOCATION_INDEX_FILE_NAME


def _lock_path(root: Path) -> Path:
    return root / JOB_LOCATION_INDEX_LOCK_NAME


def _record_to_dict(record: JobLocationRecord) -> dict[str, Any]:
    return asdict(record)


def _normalize_resource_payload(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}

    normalized: dict[str, int] = {}
    for key, value in raw.items():
        normalized_key = str(key).strip()
        if not normalized_key:
            continue
        normalized[normalized_key] = coerce_int(value, default=0) if value is not None else 0
    return normalized


def _record_from_dict(raw: dict[str, Any]) -> JobLocationRecord:
    return JobLocationRecord(
        job_id=_normalize_text(raw.get("job_id", "")),
        app_name=_normalize_text(raw.get("app_name", "")),
        job_type=_normalize_text(raw.get("job_type", "")),
        status=_normalize_text(raw.get("status", "")),
        original_run_dir=_normalize_text(raw.get("original_run_dir", "")),
        molecule_key=_normalize_text(raw.get("molecule_key", "")),
        selected_input_xyz=_normalize_text(raw.get("selected_input_xyz", "")),
        latest_known_path=_normalize_text(raw.get("latest_known_path", "")),
        resource_request=_normalize_resource_payload(raw.get("resource_request")),
        resource_actual=_normalize_resource_payload(raw.get("resource_actual")),
    )


def _load_records(root: Path) -> list[JobLocationRecord]:
    path = _index_path(root)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise JobLocationIndexCorruptError(f"Job location index cannot be read: {path}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JobLocationIndexCorruptError(f"Job location index is not valid JSON: {path}") from exc
    if not isinstance(raw, list):
        raise JobLocationIndexCorruptError(f"Job location index must contain a JSON list: {path}")
    return [_record_from_dict(item) for item in raw if isinstance(item, dict)]


def _save_records(root: Path, records: list[JobLocationRecord]) -> None:
    atomic_write_json(
        _index_path(root),
        [_record_to_dict(record) for record in records],
        ensure_ascii=True,
        indent=2,
    )


def _resolve_candidate_path(path_text: str) -> Path | None:
    raw = _normalize_text(path_text)
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def _record_paths(record: JobLocationRecord) -> list[Path]:
    paths: list[Path] = []
    for value in (
        record.original_run_dir,
        record.selected_input_xyz,
        record.latest_known_path,
    ):
        candidate = _resolve_candidate_path(value)
        if candidate is not None and candidate not in paths:
            paths.append(candidate)
    return paths


def list_job_locations(root: str | Path) -> list[JobLocationRecord]:
    resolved_root = resolve_root_path(root)
    with tmpfs_file_lock(_lock_path(resolved_root)):
        return _load_records(resolved_root)


def get_job_location(root: str | Path, job_id: str) -> JobLocationRecord | None:
    target = _normalize_text(job_id)
    if not target:
        return None
    resolved_root = resolve_root_path(root)
    with tmpfs_file_lock(_lock_path(resolved_root)):
        for record in _load_records(resolved_root):
            if record.job_id == target:
                return record
    return None


def upsert_job_location(root: str | Path, record: JobLocationRecord) -> JobLocationRecord:
    resolved_root = resolve_root_path(root)
    with tmpfs_file_lock(_lock_path(resolved_root)):
        records = _load_records(resolved_root)
        replacement = JobLocationRecord(
            job_id=_normalize_text(record.job_id),
            app_name=_normalize_text(record.app_name),
            job_type=_normalize_text(record.job_type),
            status=_normalize_text(record.status),
            original_run_dir=_normalize_text(record.original_run_dir),
            molecule_key=_normalize_text(record.molecule_key),
            selected_input_xyz=_normalize_text(record.selected_input_xyz),
            latest_known_path=_normalize_text(record.latest_known_path),
            resource_request=_normalize_resource_payload(record.resource_request),
            resource_actual=_normalize_resource_payload(record.resource_actual),
        )

        updated = False
        for index, existing in enumerate(records):
            if existing.job_id != replacement.job_id:
                continue
            records[index] = replacement
            updated = True
            break
        if not updated:
            records.append(replacement)
        _save_records(resolved_root, records)
        return replacement


def resolve_job_location(root: str | Path, lookup_target: str) -> JobLocationRecord | None:
    target = _normalize_text(lookup_target)
    if not target:
        return None

    resolved_root = resolve_root_path(root)
    candidate_path = _resolve_candidate_path(target)

    with tmpfs_file_lock(_lock_path(resolved_root)):
        records = _load_records(resolved_root)

    for record in records:
        if record.job_id == target:
            return record

    if candidate_path is None:
        return None

    for record in records:
        if candidate_path in _record_paths(record):
            return record
    return None
