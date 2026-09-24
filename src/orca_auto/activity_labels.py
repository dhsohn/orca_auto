from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from orca_auto.core import statuses as _s
from orca_auto.core.statuses import QUEUE_ACTIVE_STATUSES
from orca_auto.core.utils import normalize_text, parse_iso_utc

# Status glyphs for the queue table and CLI summaries. Keyed by the same
# ``core.statuses`` constants as ``core.terminal``'s colour map so a status is
# always drawn with one icon and one colour.
_ACTIVITY_STATUS_ICONS = {
    _s.STATUS_CREATED: "🆕",
    _s.STATUS_PENDING: "⏳",
    _s.STATUS_QUEUED: "⏳",
    _s.STATUS_RUNNING: "▶",
    _s.STATUS_RETRYING: "🔄",
    _s.STATUS_CANCEL_REQUESTED: "⏹",
    _s.STATUS_COMPLETED: "✅",
    _s.STATUS_FAILED: "❌",
    _s.STATUS_REPAIR_BLOCKED: "❌",
    _s.STATUS_CANCELLED: "⛔",
    _s.STATUS_ERROR: "❌",
}

_FALLBACK_ICON = "•"

_ORCA_SELECTED_INP_HINTS = (
    ("neb", "NEB"),
    ("irc", "IRC"),
    ("ts", "TS"),
    ("opt", "Opt"),
    ("freq", "Freq"),
)


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


def queue_elapsed_text(
    item: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    started_at = queue_elapsed_started_at(item)
    if started_at is None:
        return "--:--:--"

    status = normalize_text(item.get("status")).lower()
    end_at = parse_iso_utc(item.get("updated_at"))
    if status in QUEUE_ACTIVE_STATUSES or end_at is None:
        end_at = now or queue_table_now()
    if end_at < started_at:
        end_at = started_at
    total_seconds = max(0, int((end_at - started_at).total_seconds()))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def activity_status_icon(status: object) -> str:
    """Return the canonical icon for a queue activity status."""

    normalized = str(status).strip().lower() if status is not None else ""
    return _ACTIVITY_STATUS_ICONS.get(normalized, _FALLBACK_ICON)


def queue_status_icon(item: dict[str, Any]) -> str:
    return activity_status_icon(item.get("status"))


def queue_task_label(task_kind: Any) -> str:
    normalized = normalize_text(task_kind).lower()
    return {
        "optts": "OptTS",
        "ts": "TS",
        "opt": "Opt",
        "sp": "SP",
        "freq": "Freq",
        "irc": "IRC",
        "neb": "NEB",
        "orca": "ORCA",
    }.get(normalized, normalize_text(task_kind))


def infer_orca_detail_from_metadata(metadata: dict[str, Any]) -> str:
    task_kind = normalize_text(metadata.get("task_kind")).lower()
    task_label = queue_task_label(task_kind)
    if task_label and task_kind not in {"orca_run_inp", "run_inp"}:
        return task_label

    job_type = normalize_text(metadata.get("job_type")).lower()
    job_type_label = queue_task_label(job_type)
    if job_type_label and job_type not in {"other", "unknown"}:
        return job_type_label
    selected_inp_name = normalize_text(
        metadata.get("selected_inp_name") or metadata.get("selected_inp")
    )
    lowered = selected_inp_name.lower()
    for marker, label in _ORCA_SELECTED_INP_HINTS:
        if marker in lowered:
            return label
    return "ORCA"


def queue_detail_text(item: dict[str, Any]) -> str:
    engine = normalize_text(item.get("engine")).lower()
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    if engine == "orca":
        detail = infer_orca_detail_from_metadata(metadata)
        if normalize_text(metadata.get("publication_blocked_reason")):
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
    "activity_status_icon",
    "queue_detail_text",
    "queue_elapsed_text",
    "queue_name_text",
    "queue_status_icon",
    "queue_table_now",
]
