from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orca_auto.core.indexing import JobLocationIndexError, JobLocationRecord, resolve_job_location
from orca_auto.core.queue.metadata import (
    mapping_metadata as queue_entry_metadata_impl,
)
from orca_auto.core.queue.metadata import (
    mapping_metadata_value as queue_entry_metadata_value_impl,
)
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.core.utils.persistence import load_json_mapping_file, load_json_mapping_list_file

from ._orca_path_helpers import direct_dir_target_impl, resolve_candidate_path_impl

QUEUE_FILE_NAME = "queue.json"
INDEX_DIR_NAME = "index"
RECORDS_FILE_NAME = "records.jsonl"

JsonPayload = dict[str, Any]
JsonPayloadList = list[JsonPayload]


def load_json_dict_impl(path: Path) -> JsonPayload:
    return load_json_mapping_file(path) or {}


def load_json_list_impl(path: Path) -> JsonPayloadList:
    return load_json_mapping_list_file(path)


def load_jsonl_records_impl(path: Path) -> JsonPayloadList:
    if not path.exists():
        return []
    records: JsonPayloadList = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            records.append(raw)
    return records


def _record_candidate_dirs(record: JobLocationRecord) -> list[Path]:
    rows: list[Path] = []
    for value in (record.latest_known_path, record.original_run_dir):
        raw = normalize_text(value)
        if not raw:
            continue
        try:
            rows.append(Path(raw).expanduser().resolve())
        except OSError:
            continue
    return rows


def _resolve_record_for_target(index_root: Path | None, target: str) -> JobLocationRecord | None:
    if index_root is None:
        return None
    try:
        return resolve_job_location(index_root, target)
    except (JobLocationIndexError, OSError, ValueError):
        return None


def resolve_job_dir_impl(
    index_root: Path | None, target: str
) -> tuple[Path | None, JobLocationRecord | None]:
    candidates: list[Path] = []
    record = _resolve_record_for_target(index_root, target)
    if record is not None:
        candidates.extend(_record_candidate_dirs(record))

    direct_target = direct_dir_target_impl(target)
    if direct_target is not None:
        candidates.append(direct_target)

    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate, record
    return direct_target, record


def _queue_entry_matches(
    entry: JsonPayload,
    *,
    target: str,
    queue_id: str,
    run_id: str,
    direct_target: Path | None,
    resolved_reaction_dir: Path | None,
) -> bool:
    entry_queue_id = normalize_text(entry.get("queue_id"))
    entry_task_id = normalize_text(entry.get("task_id"))
    entry_run_id = normalize_text(queue_entry_metadata_value_impl(entry, "run_id"))
    entry_reaction_dir = resolve_candidate_path_impl(
        normalize_text(queue_entry_metadata_value_impl(entry, "reaction_dir"))
    )

    return (
        (bool(queue_id) and entry_queue_id == queue_id)
        or (bool(target) and entry_queue_id == target)
        or (bool(target) and entry_task_id == target)
        or (bool(run_id) and entry_run_id == run_id)
        or (bool(target) and entry_run_id == target)
        or (resolved_reaction_dir is not None and entry_reaction_dir == resolved_reaction_dir)
        or (direct_target is not None and entry_reaction_dir == direct_target)
    )


def find_queue_entry_impl(
    *,
    allowed_root: Path | None,
    target: str,
    queue_id: str,
    run_id: str,
    reaction_dir: str,
) -> JsonPayload | None:
    if allowed_root is None:
        return None
    entries = load_json_list_impl(allowed_root / QUEUE_FILE_NAME)
    if not entries:
        return None

    direct_target = direct_dir_target_impl(target)
    resolved_reaction_dir = resolve_candidate_path_impl(reaction_dir)

    for entry in reversed(entries):
        if _queue_entry_matches(
            entry,
            target=target,
            queue_id=queue_id,
            run_id=run_id,
            direct_target=direct_target,
            resolved_reaction_dir=resolved_reaction_dir,
        ):
            return entry
    return None


__all__ = [
    "find_queue_entry_impl",
    "load_json_dict_impl",
    "load_json_list_impl",
    "load_jsonl_records_impl",
    "queue_entry_metadata_impl",
    "queue_entry_metadata_value_impl",
    "resolve_job_dir_impl",
]
