from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from orca_auto.core.statuses import is_queue_active_status
from orca_auto.core.utils import normalize_text, parse_iso_utc
from orca_auto.orca.app_ids import ORCA_TASK_KIND
from orca_auto.orca.queue.terminal_marker import TERMINAL_PUBLICATION_SCOPE
from orca_auto.orca.queue_detail import QUEUE_DETAIL_KIND_KEY, queue_detail_kind_label

_UNKNOWN_DETAIL_LABEL = "Unknown"

# Legacy queue ``task_kind`` / coarse ``job_type`` tokens that name an operation.
_RECOGNIZED_OPERATION_LABELS: dict[str, str] = {
    "optts": "OptTS",
    "ts": "TS",
    "opt": "Opt",
    "sp": "SP",
    "freq": "Freq",
    "irc": "IRC",
    "neb": "NEB",
}
_GENERIC_TASK_KINDS = frozenset({ORCA_TASK_KIND, "run_inp", "orca"})


def queue_table_now() -> datetime:
    return datetime.now(UTC)


def queue_elapsed_started_at(item: dict[str, Any]) -> datetime | None:
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    restart_summary = metadata.get("restart_summary")
    restart_summary = restart_summary if isinstance(restart_summary, dict) else {}
    for value in (
        metadata.get("elapsed_started_at"),
        metadata.get("last_restarted_at"),
        restart_summary.get("restarted_at"),
        item.get("submitted_at"),
        item.get("updated_at"),
    ):
        parsed = parse_iso_utc(value)
        if parsed is not None:
            return parsed
    return None


def queue_elapsed_text(item: dict[str, Any], *, now: datetime) -> str:
    started_at = queue_elapsed_started_at(item)
    if started_at is None:
        return "--:--:--"

    end_at = parse_iso_utc(item.get("updated_at"))
    if is_queue_active_status(item.get("status")) or end_at is None:
        end_at = now
    if end_at < started_at:
        end_at = started_at
    total_seconds = max(0, int((end_at - started_at).total_seconds()))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def recognized_queue_operation_label(kind: Any) -> str | None:
    """Fixed Detail label for a known ``task_kind``/``job_type`` token, else ``None``."""
    normalized = normalize_text(kind).lower()
    if not normalized or normalized in _GENERIC_TASK_KINDS:
        return None
    if normalized in {"other", "unknown"}:
        return None
    return _RECOGNIZED_OPERATION_LABELS.get(normalized)


def infer_orca_detail_from_metadata(metadata: dict[str, Any]) -> str:
    task_label = recognized_queue_operation_label(metadata.get("task_kind"))
    if task_label is not None:
        return task_label

    detail_label = queue_detail_kind_label(normalize_text(metadata.get(QUEUE_DETAIL_KIND_KEY)))
    if detail_label is not None:
        return detail_label

    job_label = recognized_queue_operation_label(metadata.get("job_type"))
    if job_label is not None:
        return job_label
    return _UNKNOWN_DETAIL_LABEL


def queue_detail_text(item: dict[str, Any]) -> str:
    engine = normalize_text(item.get("engine")).lower()
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    if engine == "orca":
        detail = infer_orca_detail_from_metadata(metadata)
        if normalize_text(metadata.get("publication_blocked_reason")):
            if metadata.get("publication_blocked_scope") == TERMINAL_PUBLICATION_SCOPE:
                return f"{detail} (result publication pending)"
            return f"{detail} (waiting for publication repair)"
        if normalize_text(metadata.get("admission_deferral_reason")):
            # The full reason is in the JSON record; the table only says why
            # a pending row is not being started.
            return f"{detail} (waiting for resources)"
        return detail
    return normalize_text(item.get("label")) or normalize_text(item.get("source")) or "-"


def queue_looks_like_path(value: str) -> bool:
    text = normalize_text(value)
    return "/" in text or "\\" in text


def queue_path_name(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    normalized = text.replace("\\", "/").rstrip("/")
    if not normalized:
        return ""
    return normalized.rsplit("/", 1)[-1]


def queue_metadata_path_name(metadata: dict[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        name = queue_path_name(metadata.get(key))
        if name and name not in {"reaction_dir"}:
            return name
    return ""


def queue_name_text(item: dict[str, Any]) -> str:
    activity_id = normalize_text(item.get("activity_id")) or "-"
    label = normalize_text(item.get("label"))
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    if label and not queue_looks_like_path(label):
        return label

    path_name = queue_metadata_path_name(
        metadata,
        (
            "reaction_dir",
            "job_dir",
            "original_run_dir",
            "latest_known_path",
        ),
    )
    if path_name:
        return path_name

    label_name = queue_path_name(label)
    if label_name and label_name != "reaction_dir":
        return label_name
    return activity_id


__all__ = [
    "queue_detail_text",
    "queue_elapsed_text",
    "queue_name_text",
    "queue_table_now",
]
