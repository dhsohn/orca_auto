from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from orca_auto.core.activity_icons import activity_status_icon
from orca_auto.core.statuses import QUEUE_ACTIVE_STATUSES
from orca_auto.core.utils import normalize_text, parse_iso_utc
from orca_auto.flow.templates import workflow_template_label

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


def queue_status_icon(item: dict[str, Any]) -> str:
    return activity_status_icon(item.get("status"))


def queue_task_label(task_kind: Any) -> str:
    normalized = normalize_text(task_kind).lower()
    return {
        "crest_conformer_search": "conformer_search",
        "conformer_search": "conformer_search",
        "path_search": "TS path",
        "xtb_path_search": "TS path",
        "optts_freq": "OptTS+Freq",
        "optts": "OptTS",
        "ts": "TS",
        "opt": "Opt",
        "sp": "SP",
        "freq": "Freq",
        "irc": "IRC",
        "neb": "NEB",
        "orca": "ORCA",
        "xtb": "xTB",
        "crest": "CREST",
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


def workflow_detail_text(metadata: dict[str, Any]) -> str:
    base = workflow_template_label(metadata.get("template_name"))
    request_parameters = metadata.get("request_parameters")
    request_parameters = request_parameters if isinstance(request_parameters, dict) else {}
    crest_mode = normalize_text(request_parameters.get("crest_mode"))
    return f"{base}({crest_mode})" if crest_mode else base


def crest_detail_text(metadata: dict[str, Any]) -> str:
    base = queue_task_label(metadata.get("task_kind")) or "conformer_search"
    mode = normalize_text(metadata.get("mode"))
    return f"{base}({mode})" if mode else base


def xtb_detail_text(metadata: dict[str, Any]) -> str:
    return (
        queue_task_label(metadata.get("task_kind"))
        or queue_task_label(metadata.get("job_type"))
        or "xTB"
    )


_QUEUE_ENGINE_DETAIL_TEXT = {
    "crest": crest_detail_text,
    "xtb": xtb_detail_text,
    "orca": infer_orca_detail_from_metadata,
}


def queue_detail_text(item: dict[str, Any]) -> str:
    kind = normalize_text(item.get("kind")).lower()
    engine = normalize_text(item.get("engine")).lower()
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    if kind == "workflow":
        return workflow_detail_text(metadata)
    if detail_text := _QUEUE_ENGINE_DETAIL_TEXT.get(engine):
        return detail_text(metadata)
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
        if name and name not in {"reaction_dir", "workflow.json"}:
            return name
    return ""


def queue_name_text(item: dict[str, Any]) -> str:
    activity_id = normalize_text(item.get("activity_id")) or "-"
    kind = normalize_text(item.get("kind")).lower()
    label = normalize_text(item.get("label"))
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    if kind == "workflow":
        # The submitted directory is the stable user-facing name: the scaffold
        # for scaffolded workflows, the workspace itself otherwise. Stage
        # labels (reaction keys, candidate paths) belong to Detail, not Name.
        workspace_name = normalize_text(
            metadata.get("workspace_display_name")
        ) or queue_metadata_path_name(metadata, ("workspace_dir", "workflow_file"))
        if workspace_name:
            return workspace_name
        if label and not queue_looks_like_path(label):
            return label
        return activity_id

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
    if label_name and label_name not in {"reaction_dir", "workflow.json"}:
        return label_name

    return activity_id


__all__ = [
    "queue_detail_text",
    "queue_elapsed_text",
    "queue_name_text",
    "queue_status_icon",
    "queue_table_now",
]
