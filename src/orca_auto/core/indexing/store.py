from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypeVar

from ..activity_index import published_source
from ..utils.coercion import normalize_text
from ..utils.lock import file_lock
from ..utils.persistence import (
    atomic_write_json,
    coerce_int,
    load_json_list_file,
    resolve_root_path,
)
from .location import JobLocationRecord

JOB_LOCATION_INDEX_FILE_NAME = "job_locations.json"
JOB_LOCATION_INDEX_LOCK_NAME = "job_locations.lock"


def normalize_index_text(value: Any) -> str:
    return normalize_text(value, none="None")


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
        job_id=normalize_index_text(raw.get("job_id", "")),
        app_name=normalize_index_text(raw.get("app_name", "")),
        job_type=normalize_index_text(raw.get("job_type", "")),
        status=normalize_index_text(raw.get("status", "")),
        original_run_dir=normalize_index_text(raw.get("original_run_dir", "")),
        molecule_key=normalize_index_text(raw.get("molecule_key", "")),
        selected_input_xyz=normalize_index_text(raw.get("selected_input_xyz", "")),
        latest_known_path=normalize_index_text(raw.get("latest_known_path", "")),
        resource_request=_normalize_resource_payload(raw.get("resource_request")),
        resource_actual=_normalize_resource_payload(raw.get("resource_actual")),
    )


def load_job_locations(root: Path) -> list[JobLocationRecord]:
    """Read the index rows without taking the index lock.

    Callers that need the rows and the file's identity to agree hold
    ``JOB_LOCATION_INDEX_LOCK_NAME`` around this call; ``list_job_locations`` is
    the self-locking reader.
    """
    raw = load_json_list_file(
        _index_path(root),
        corrupt_error=JobLocationIndexCorruptError,
        description="Job location index",
    )
    return [_record_from_dict(item) for item in raw if isinstance(item, dict)]


def _save_records(root: Path, records: list[JobLocationRecord]) -> None:
    payload = [_record_to_dict(record) for record in records]
    atomic_write_json(
        _index_path(root),
        payload,
        ensure_ascii=True,
        indent=2,
    )
    published_source(root, "location", JOB_LOCATION_INDEX_FILE_NAME, payload)


def _resolve_candidate_path(path_text: str) -> Path | None:
    raw = normalize_index_text(path_text)
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


@dataclass(frozen=True)
class JobLocationPruneResult:
    index_path: str
    total: int
    pruned: tuple[JobLocationRecord, ...]
    applied: bool


def _absolute_record_paths(record: JobLocationRecord) -> list[Path]:
    """Recorded paths that disk can vouch for: absolute after ``~`` expansion.

    A relative value would be judged against the caller's working directory,
    and a JSON ``null`` loads as the text ``None``; neither can prove a row
    stale, so both are left out and cannot make a row prunable.
    """
    paths: list[Path] = []
    for value in (
        record.original_run_dir,
        record.selected_input_xyz,
        record.latest_known_path,
    ):
        raw = normalize_index_text(value)
        if not raw or not Path(raw).expanduser().is_absolute():
            continue
        candidate = _resolve_candidate_path(raw)
        if candidate is not None and candidate not in paths:
            paths.append(candidate)
    return paths


def _record_paths_survive(record: JobLocationRecord) -> bool:
    """True when the record names no absolute path, or any named one still exists."""
    paths = _absolute_record_paths(record)
    if not paths:
        return True
    return any(path.exists() for path in paths)


def prune_job_locations(root: str | Path, *, apply: bool) -> JobLocationPruneResult:
    """Drop the rows whose every recorded absolute path is gone from disk.

    A row that records at least one absolute ``original_run_dir``,
    ``selected_input_xyz`` or ``latest_known_path`` and finds none of them on
    disk can resolve neither a job directory nor a path alias any more; it is
    reachable by job id only and then leads nowhere. A row that records no
    absolute path is kept, because nothing on disk can prove it stale. Without
    ``apply`` the index is left untouched and the result only reports what
    would go.
    """
    resolved_root = resolve_root_path(root)
    with file_lock(_lock_path(resolved_root)):
        records = load_job_locations(resolved_root)
        kept: list[JobLocationRecord] = []
        pruned: list[JobLocationRecord] = []
        for record in records:
            (kept if _record_paths_survive(record) else pruned).append(record)
        applied = bool(apply and pruned)
        if applied:
            _save_records(resolved_root, kept)
    return JobLocationPruneResult(
        index_path=str(_index_path(resolved_root)),
        total=len(records),
        pruned=tuple(pruned),
        applied=applied,
    )


def list_job_locations(root: str | Path) -> list[JobLocationRecord]:
    resolved_root = resolve_root_path(root)
    with file_lock(_lock_path(resolved_root)):
        return load_job_locations(resolved_root)


def get_job_location(root: str | Path, job_id: str) -> JobLocationRecord | None:
    target = normalize_index_text(job_id)
    if not target:
        return None
    resolved_root = resolve_root_path(root)
    with file_lock(_lock_path(resolved_root)):
        for record in load_job_locations(resolved_root):
            if record.job_id == target:
                return record
    return None


def _normalized_record(record: JobLocationRecord) -> JobLocationRecord:
    return JobLocationRecord(
        job_id=normalize_index_text(record.job_id),
        app_name=normalize_index_text(record.app_name),
        job_type=normalize_index_text(record.job_type),
        status=normalize_index_text(record.status),
        original_run_dir=normalize_index_text(record.original_run_dir),
        molecule_key=normalize_index_text(record.molecule_key),
        selected_input_xyz=normalize_index_text(record.selected_input_xyz),
        latest_known_path=normalize_index_text(record.latest_known_path),
        resource_request=_normalize_resource_payload(record.resource_request),
        resource_actual=_normalize_resource_payload(record.resource_actual),
    )


@dataclass(frozen=True)
class JobLocationUpsertResult:
    index_path: str
    total: int
    added: tuple[JobLocationRecord, ...]
    updated: tuple[JobLocationRecord, ...]
    unchanged: int
    applied: bool


_CandidateT = TypeVar("_CandidateT")


def _merge_locked(
    resolved_root: Path,
    items: Iterable[tuple[str, _CandidateT]],
    *,
    decide: Callable[[JobLocationRecord | None, _CandidateT], JobLocationRecord | None],
    apply: bool,
) -> JobLocationUpsertResult:
    """One lock, one write: ``decide`` sees each job id's row as it is right now.

    ``decide(existing, candidate)`` returns the row to store, or ``None`` to
    leave that job id alone (counted as unchanged). A replacement equal to the
    loaded row is unchanged too; when nothing changes the file bytes are left
    exactly as they were.
    """
    with file_lock(_lock_path(resolved_root)):
        merged = load_job_locations(resolved_root)
        slots: dict[str, int] = {}
        for index, existing in enumerate(merged):
            slots.setdefault(existing.job_id, index)
        added: list[JobLocationRecord] = []
        updated: list[JobLocationRecord] = []
        unchanged = 0
        for job_id, candidate in items:
            key = normalize_index_text(job_id)
            slot = slots.get(key)
            current = merged[slot] if slot is not None else None
            decided = decide(current, candidate)
            if decided is None:
                unchanged += 1
                continue
            replacement = _normalized_record(decided)
            if replacement.job_id != key:
                raise JobLocationIndexError(
                    f"Job location merge changed the job id: {key!r} -> {replacement.job_id!r}"
                )
            if slot is None:
                slots[key] = len(merged)
                merged.append(replacement)
                added.append(replacement)
            elif merged[slot] == replacement:
                unchanged += 1
            else:
                merged[slot] = replacement
                updated.append(replacement)
        applied = bool(apply and (added or updated))
        if applied:
            _save_records(resolved_root, merged)
    return JobLocationUpsertResult(
        index_path=str(_index_path(resolved_root)),
        total=len(merged),
        added=tuple(added),
        updated=tuple(updated),
        unchanged=unchanged,
        applied=applied,
    )


def upsert_job_locations(
    root: str | Path, records: Iterable[JobLocationRecord], *, apply: bool = True
) -> JobLocationUpsertResult:
    """Merge ``records`` into the index by job id under one lock and one write.

    A row whose job id is already indexed is replaced in place; a new job id is
    appended, so submission order is preserved for path-alias resolution. A
    replacement equal to the loaded row is counted as unchanged and, when no
    row changes at all, the file bytes are left exactly as they were (no
    atomic rewrite, no projection publication). Without ``apply`` nothing is
    written and the result only reports what would change. A caller that must
    compare against the row as it is at write time uses
    ``merge_job_locations`` instead.
    """
    normalized = [_normalized_record(record) for record in records]
    return _merge_locked(
        resolve_root_path(root),
        ((record.job_id, record) for record in normalized),
        decide=lambda _existing, record: record,
        apply=apply,
    )


def merge_job_locations(
    root: str | Path,
    candidates: Mapping[str, _CandidateT],
    *,
    decide: Callable[[JobLocationRecord | None, _CandidateT], JobLocationRecord | None],
    apply: bool = True,
) -> JobLocationUpsertResult:
    """Decide each job id's row under the index lock, against the row as it is then.

    ``candidates`` maps a job id to whatever the caller collected for it
    without the lock; ``decide(existing, candidate)`` is called inside the lock
    with the currently indexed row (``None`` when the id is unindexed) and
    returns the row to store, or ``None`` to leave the index alone for that
    id. A row that lands between the caller's plan and this merge is therefore
    seen by ``decide`` rather than overwritten. Counting and writing follow
    ``upsert_job_locations``.
    """
    return _merge_locked(
        resolve_root_path(root),
        candidates.items(),
        decide=decide,
        apply=apply,
    )


def upsert_job_location(root: str | Path, record: JobLocationRecord) -> JobLocationRecord:
    replacement = _normalized_record(record)
    upsert_job_locations(root, (replacement,))
    return replacement


def resolve_job_location(root: str | Path, lookup_target: str) -> JobLocationRecord | None:
    target = normalize_index_text(lookup_target)
    if not target:
        return None

    resolved_root = resolve_root_path(root)
    candidate_path = _resolve_candidate_path(target)

    with file_lock(_lock_path(resolved_root)):
        records = load_job_locations(resolved_root)

    for record in records:
        if record.job_id == target:
            return record

    if candidate_path is None:
        return None

    # Multiple immutable generations may share one submitted job directory.
    # Records are appended in submission order, so a path alias must select the
    # newest matching job while an exact job id above remains historical.
    for record in reversed(records):
        if candidate_path in _record_paths(record):
            return record
    return None
