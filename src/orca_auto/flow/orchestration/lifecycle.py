from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orca_auto.core.statuses import (
    STATUS_CANCEL_REQUESTED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PLANNED,
    STATUS_RUNNING,
    WORKFLOW_FAILED_STATUSES,
    is_queue_active_status,
    is_stage_terminal_status,
    is_sync_only_workflow_status,
)


def workflow_sync_only_impl(
    payload: dict[str, Any], *, normalize_text_fn: Callable[[Any], str]
) -> bool:
    status = normalize_text_fn(payload.get("status")).lower()
    if status == STATUS_COMPLETED and _conformer_orca_handoff_pending_raw(
        payload,
        normalize_text_fn=normalize_text_fn,
    ):
        return False
    return is_sync_only_workflow_status(status)


def workflow_has_active_children_impl(
    payload: dict[str, Any],
    *,
    normalize_text_fn: Callable[[Any], str],
    workflow_has_active_downstream_fn: Callable[[dict[str, Any]], bool],
) -> bool:
    for raw_stage in payload.get("stages", []):
        if not isinstance(raw_stage, dict):
            continue
        stage_status = normalize_text_fn(raw_stage.get("status")).lower()
        if is_queue_active_status(stage_status):
            return True
        task = raw_stage.get("task")
        if not isinstance(task, dict):
            continue
        task_status = normalize_text_fn(task.get("status")).lower()
        if is_queue_active_status(task_status):
            return True
    return workflow_has_active_downstream_fn(payload)


def latest_child_stage_summary_impl(
    stage_summaries: list[dict[str, Any]],
    *,
    normalize_text_fn: Callable[[Any], str],
) -> dict[str, Any]:
    if not stage_summaries:
        return {}
    priority = {
        "running": 5,
        "submitted": 4,
        "queued": 3,
        "planned": 2,
        "cancel_requested": 1,
    }
    chosen = stage_summaries[-1]
    best_priority = -1
    for item in stage_summaries:
        status = normalize_text_fn(item.get("status")).lower()
        task_status = normalize_text_fn(item.get("task_status")).lower()
        score = max(priority.get(status, 0), priority.get(task_status, 0))
        if score >= best_priority:
            best_priority = score
            chosen = item
    return {
        "stage_id": normalize_text_fn(chosen.get("stage_id")),
        "stage_kind": normalize_text_fn(chosen.get("stage_kind")),
        "engine": normalize_text_fn(chosen.get("engine")),
        "task_kind": normalize_text_fn(chosen.get("task_kind")),
        "status": normalize_text_fn(chosen.get("status")),
        "task_status": normalize_text_fn(chosen.get("task_status")),
        "analyzer_status": normalize_text_fn(chosen.get("analyzer_status")),
        "reason": normalize_text_fn(chosen.get("reason")),
        "queue_id": normalize_text_fn(chosen.get("queue_id")),
        "run_id": normalize_text_fn(chosen.get("run_id")),
        "latest_known_path": normalize_text_fn(chosen.get("latest_known_path")),
        "organized_output_dir": normalize_text_fn(chosen.get("organized_output_dir")),
        "completed_at": normalize_text_fn(chosen.get("completed_at")),
    }


def downstream_terminal_result_impl(
    child_payload: dict[str, Any],
    child_summary: dict[str, Any],
    *,
    normalize_text_fn: Callable[[Any], str],
) -> dict[str, Any]:
    status = normalize_text_fn(child_summary.get("status")).lower()
    if not is_stage_terminal_status(status):
        return {}
    metadata = child_payload.get("metadata")
    workflow_error: dict[str, Any] = {}
    if isinstance(metadata, dict) and isinstance(metadata.get("workflow_error"), dict):
        workflow_error = metadata.get("workflow_error") or {}
    last_completed_at = ""
    for stage in child_summary.get("stage_summaries", []):
        if not isinstance(stage, dict):
            continue
        completed_at = normalize_text_fn(stage.get("completed_at"))
        if completed_at:
            last_completed_at = completed_at
    return {
        "status": normalize_text_fn(child_summary.get("status")),
        "completed_at": last_completed_at,
        "failure_reason": normalize_text_fn(workflow_error.get("reason")),
        "failure_scope": normalize_text_fn(workflow_error.get("scope")),
    }


def stage_failure_is_recoverable_impl(
    stage: dict[str, Any],
    *,
    normalize_text_fn: Callable[[Any], str],
    stage_metadata_fn: Callable[[dict[str, Any]], dict[str, Any]],
) -> bool:
    status = normalize_text_fn(stage.get("status")).lower()
    if status not in WORKFLOW_FAILED_STATUSES:
        return False
    task = stage.get("task")
    if not isinstance(task, dict):
        return False
    engine = normalize_text_fn(task.get("engine"))
    metadata = stage_metadata_fn(stage)
    if engine == "xtb":
        return normalize_text_fn(metadata.get("reaction_handoff_status")) == "ready"
    if engine == "orca":
        return normalize_text_fn(metadata.get("reaction_candidate_status")) == "superseded"
    return False


def effective_stage_status_impl(
    stage: dict[str, Any],
    *,
    normalize_text_fn: Callable[[Any], str],
    stage_failure_is_recoverable_fn: Callable[[dict[str, Any]], bool],
) -> str:
    if stage_failure_is_recoverable_fn(stage):
        return "completed"
    return normalize_text_fn(stage.get("status")).lower()


def _workflow_error_is_failed(
    payload: dict[str, Any],
    *,
    normalize_text_fn: Callable[[Any], str],
) -> bool:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return False
    workflow_error = metadata.get("workflow_error")
    return (
        isinstance(workflow_error, dict)
        and normalize_text_fn(workflow_error.get("status")).lower() == "failed"
    )


def _stage_engine(stage: dict[str, Any], *, normalize_text_fn: Callable[[Any], str]) -> str:
    task = stage.get("task")
    if not isinstance(task, dict):
        return ""
    return normalize_text_fn(task.get("engine")).lower()


def _template_name(payload: dict[str, Any], *, normalize_text_fn: Callable[[Any], str]) -> str:
    return normalize_text_fn(payload.get("template_name")).lower()


def _stage_task_status(stage: dict[str, Any], *, normalize_text_fn: Callable[[Any], str]) -> str:
    task = stage.get("task")
    if not isinstance(task, dict):
        return ""
    return normalize_text_fn(task.get("status")).lower()


def _conformer_orca_handoff_pending_raw(
    payload: dict[str, Any],
    *,
    normalize_text_fn: Callable[[Any], str],
) -> bool:
    if _template_name(payload, normalize_text_fn=normalize_text_fn) != "conformer_screening":
        return False
    has_orca_stage = False
    has_completed_crest_stage = False
    for raw_stage in payload.get("stages", []):
        if not isinstance(raw_stage, dict):
            continue
        engine = _stage_engine(raw_stage, normalize_text_fn=normalize_text_fn)
        if engine == "orca":
            has_orca_stage = True
            break
        if engine != "crest":
            continue
        stage_status = normalize_text_fn(raw_stage.get("status")).lower()
        task_status = _stage_task_status(raw_stage, normalize_text_fn=normalize_text_fn)
        if stage_status == STATUS_COMPLETED and task_status in {"", STATUS_COMPLETED}:
            has_completed_crest_stage = True
    return has_completed_crest_stage and not has_orca_stage


def _conformer_orca_handoff_pending(
    payload: dict[str, Any],
    stage_rows: list[tuple[dict[str, Any], str, str]],
    *,
    normalize_text_fn: Callable[[Any], str],
) -> bool:
    if _template_name(payload, normalize_text_fn=normalize_text_fn) != "conformer_screening":
        return False
    has_orca_stage = any(engine == "orca" for _, _, engine in stage_rows)
    has_completed_crest_stage = any(
        engine == "crest" and status == STATUS_COMPLETED for _, status, engine in stage_rows
    )
    return has_completed_crest_stage and not has_orca_stage


def _workflow_status_from_stage_statuses(
    *,
    stages: list[dict[str, Any]],
    statuses: list[str],
    current_status: str,
) -> str:
    if current_status == STATUS_CANCELLED:
        return STATUS_CANCELLED
    if current_status == STATUS_CANCEL_REQUESTED:
        return (
            STATUS_CANCEL_REQUESTED
            if any(is_queue_active_status(status) for status in statuses)
            else STATUS_CANCELLED
        )
    if any(is_queue_active_status(status) for status in statuses):
        return STATUS_RUNNING
    if any(status == STATUS_PLANNED for status in statuses):
        return STATUS_RUNNING
    if stages and all(is_stage_terminal_status(status) for status in statuses):
        return STATUS_COMPLETED
    if any(status == STATUS_COMPLETED for status in statuses):
        return STATUS_RUNNING
    return STATUS_PLANNED


def recompute_workflow_status_impl(
    payload: dict[str, Any],
    *,
    normalize_text_fn: Callable[[Any], str],
    effective_stage_status_fn: Callable[[dict[str, Any]], str],
) -> str:
    stages = [stage for stage in payload.get("stages", []) if isinstance(stage, dict)]
    stage_rows = [
        (
            stage,
            effective_stage_status_fn(stage),
            _stage_engine(stage, normalize_text_fn=normalize_text_fn),
        )
        for stage in stages
    ]
    statuses = [status for _, status, _ in stage_rows]
    current_status = normalize_text_fn(payload.get("status")).lower()
    if _workflow_error_is_failed(payload, normalize_text_fn=normalize_text_fn):
        return "failed"
    if any(
        status in WORKFLOW_FAILED_STATUSES and engine in {"", "crest"}
        for _, status, engine in stage_rows
    ):
        return "failed"
    if current_status not in {
        STATUS_CANCELLED,
        STATUS_CANCEL_REQUESTED,
    } and _conformer_orca_handoff_pending(
        payload,
        stage_rows,
        normalize_text_fn=normalize_text_fn,
    ):
        return STATUS_RUNNING
    return _workflow_status_from_stage_statuses(
        stages=stages,
        statuses=statuses,
        current_status=current_status,
    )


__all__ = [
    "downstream_terminal_result_impl",
    "effective_stage_status_impl",
    "latest_child_stage_summary_impl",
    "recompute_workflow_status_impl",
    "stage_failure_is_recoverable_impl",
    "workflow_has_active_children_impl",
    "workflow_sync_only_impl",
]
