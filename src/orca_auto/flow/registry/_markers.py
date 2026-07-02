from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.utils import normalize_text


def normal_path_key(value: str) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except OSError:
        return text


def cleared_record_keys(record: Any) -> set[str]:
    keys = {normalize_text(record.workflow_id)}
    keys.add(normal_path_key(record.workspace_dir))
    keys.add(normal_path_key(record.workflow_file))
    return {key for key in keys if key}


def marker_keys(marker: dict[str, Any]) -> set[str]:
    keys = {normalize_text(marker.get("workflow_id"))}
    keys.add(normal_path_key(normalize_text(marker.get("workspace_dir"))))
    keys.add(normal_path_key(normalize_text(marker.get("workflow_file"))))
    return {key for key in keys if key}


def cleared_marker_identity(marker: dict[str, Any]) -> str:
    return (
        normalize_text(marker.get("workflow_id"))
        or normal_path_key(normalize_text(marker.get("workspace_dir")))
        or normal_path_key(normalize_text(marker.get("workflow_file")))
    )


def record_clear_identity(record: Any) -> str:
    return (
        normalize_text(record.workflow_id)
        or normal_path_key(record.workspace_dir)
        or normal_path_key(record.workflow_file)
    )


def record_matches_cleared_marker(record: Any, markers: list[dict[str, Any]]) -> bool:
    keys = cleared_record_keys(record)
    return any(keys & marker_keys(marker) for marker in markers)


def record_is_clearable_terminal(
    record: Any,
    statuses: set[str] | frozenset[str],
) -> bool:
    target_statuses = {
        normalize_text(status).lower() for status in statuses if normalize_text(status)
    }
    return normalize_text(record.status).lower() in target_statuses


def remove_matching_cleared_markers(
    markers: list[dict[str, Any]],
    record: Any,
) -> tuple[list[dict[str, Any]], bool]:
    keys = cleared_record_keys(record)
    kept = [marker for marker in markers if not keys & marker_keys(marker)]
    return kept, len(kept) != len(markers)


def add_cleared_markers(
    markers: list[dict[str, Any]],
    records: list[Any],
    *,
    cleared_at: str,
) -> tuple[list[dict[str, Any]], bool]:
    by_key: dict[str, dict[str, Any]] = {}
    for marker in markers:
        marker_key = cleared_marker_identity(marker)
        if not marker_key:
            continue
        by_key[marker_key] = marker

    changed = False
    for record in records:
        marker = {
            "workflow_id": normalize_text(record.workflow_id),
            "status": normalize_text(record.status).lower(),
            "workspace_dir": normal_path_key(record.workspace_dir),
            "workflow_file": normal_path_key(record.workflow_file),
            "cleared_at": cleared_at,
        }
        key = record_clear_identity(record)
        if not key:
            continue
        if by_key.get(key) != marker:
            changed = True
        by_key[key] = marker
    return list(by_key.values()), changed


def filter_cleared_terminal_records(
    records: list[Any],
    markers: list[dict[str, Any]],
    *,
    terminal_statuses: set[str] | frozenset[str],
) -> list[Any]:
    return [
        record
        for record in records
        if not (
            record_is_clearable_terminal(record, terminal_statuses)
            and record_matches_cleared_marker(record, markers)
        )
    ]


__all__ = [
    "add_cleared_markers",
    "cleared_record_keys",
    "filter_cleared_terminal_records",
    "normal_path_key",
    "record_is_clearable_terminal",
    "record_matches_cleared_marker",
    "remove_matching_cleared_markers",
]
