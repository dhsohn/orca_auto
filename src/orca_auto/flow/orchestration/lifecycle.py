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
from orca_auto.core.utils.coercion import normalize_text


def workflow_sync_only_impl(payload: dict[str, Any]) -> bool:
    status = normalize_text(payload.get("status")).lower()
    if status == STATUS_COMPLETED and _conformer_orca_handoff_pending_raw(payload):
        return False
    return is_sync_only_workflow_status(status)


def workflow_has_active_children_impl(
    payload: dict[str, Any],
    *,
    workflow_has_active_downstream_fn: Callable[[dict[str, Any]], bool],
) -> bool:
    for raw_stage in payload.get("stages", []):
        if not isinstance(raw_stage, dict):
            continue
        stage_status = normalize_text(raw_stage.get("status")).lower()
        if is_queue_active_status(stage_status):
            return True
        task = raw_stage.get("task")
        if not isinstance(task, dict):
            continue
        task_status = normalize_text(task.get("status")).lower()
        if is_queue_active_status(task_status):
            return True
    return workflow_has_active_downstream_fn(payload)


def stage_failure_is_recoverable_impl(
    stage: dict[str, Any],
    *,
    stage_metadata_fn: Callable[[dict[str, Any]], dict[str, Any]],
) -> bool:
    status = normalize_text(stage.get("status")).lower()
    if status not in WORKFLOW_FAILED_STATUSES:
        return False
    task = stage.get("task")
    if not isinstance(task, dict):
        return False
    engine = normalize_text(task.get("engine"))
    metadata = stage_metadata_fn(stage)
    if engine == "xtb":
        return normalize_text(metadata.get("reaction_handoff_status")) == "ready"
    return False


def effective_stage_status_impl(
    stage: dict[str, Any],
    *,
    stage_failure_is_recoverable_fn: Callable[[dict[str, Any]], bool],
) -> str:
    if stage_failure_is_recoverable_fn(stage):
        return "completed"
    return normalize_text(stage.get("status")).lower()


def _workflow_error_is_failed(payload: dict[str, Any]) -> bool:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return False
    workflow_error = metadata.get("workflow_error")
    return (
        isinstance(workflow_error, dict)
        and normalize_text(workflow_error.get("status")).lower() == "failed"
    )


def _stage_engine(stage: dict[str, Any]) -> str:
    task = stage.get("task")
    if not isinstance(task, dict):
        return ""
    return normalize_text(task.get("engine")).lower()


def _stage_is_workflow_fatal(stage: dict[str, Any]) -> bool:
    """A prerequisite stage (e.g. the scan_ts_search relaxed scan) whose failure
    must fail the whole workflow, unlike ordinary candidate ORCA stages."""
    metadata = stage.get("metadata")
    return bool(metadata.get("workflow_fatal")) if isinstance(metadata, dict) else False


def _template_name(payload: dict[str, Any]) -> str:
    return normalize_text(payload.get("template_name")).lower()


def _stage_task_status(stage: dict[str, Any]) -> str:
    task = stage.get("task")
    if not isinstance(task, dict):
        return ""
    return normalize_text(task.get("status")).lower()


def _conformer_orca_handoff_pending_raw(payload: dict[str, Any]) -> bool:
    if _template_name(payload) != "conformer_screening":
        return False
    has_orca_stage = False
    has_completed_crest_stage = False
    for raw_stage in payload.get("stages", []):
        if not isinstance(raw_stage, dict):
            continue
        engine = _stage_engine(raw_stage)
        if engine == "orca":
            has_orca_stage = True
            break
        if engine != "crest":
            continue
        stage_status = normalize_text(raw_stage.get("status")).lower()
        task_status = _stage_task_status(raw_stage)
        if stage_status == STATUS_COMPLETED and task_status in {"", STATUS_COMPLETED}:
            has_completed_crest_stage = True
    return has_completed_crest_stage and not has_orca_stage


def _conformer_orca_handoff_pending(
    payload: dict[str, Any],
    stage_rows: list[tuple[dict[str, Any], str, str]],
) -> bool:
    if _template_name(payload) != "conformer_screening":
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
    if current_status in WORKFLOW_FAILED_STATUSES:
        return current_status
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
        # A stage-level cancellation must never read as success: cancelled
        # candidates carry no TS/conformer verdict, and the exhaustion
        # recorders deliberately stand down when cancels are present — so
        # without this, cancelling every candidate ends the workflow
        # COMPLETED. The workflow did not run to its plan; say CANCELLED.
        if any(status == STATUS_CANCELLED for status in statuses):
            return STATUS_CANCELLED
        return STATUS_COMPLETED
    if any(status == STATUS_COMPLETED for status in statuses):
        return STATUS_RUNNING
    return STATUS_PLANNED


def recompute_workflow_status_impl(
    payload: dict[str, Any],
    *,
    effective_stage_status_fn: Callable[[dict[str, Any]], str],
) -> str:
    stages = [stage for stage in payload.get("stages", []) if isinstance(stage, dict)]
    stage_rows = [
        (stage, effective_stage_status_fn(stage), _stage_engine(stage)) for stage in stages
    ]
    statuses = [status for _, status, _ in stage_rows]
    current_status = normalize_text(payload.get("status")).lower()
    if _workflow_error_is_failed(payload):
        return "failed"
    if any(
        status in WORKFLOW_FAILED_STATUSES
        and (engine in {"", "crest"} or _stage_is_workflow_fatal(stage))
        for stage, status, engine in stage_rows
    ):
        return "failed"
    if current_status not in {
        STATUS_CANCELLED,
        STATUS_CANCEL_REQUESTED,
    } and _conformer_orca_handoff_pending(payload, stage_rows):
        return STATUS_RUNNING
    return _workflow_status_from_stage_statuses(
        stages=stages,
        statuses=statuses,
        current_status=current_status,
    )


__all__ = [
    "effective_stage_status_impl",
    "recompute_workflow_status_impl",
    "stage_failure_is_recoverable_impl",
    "workflow_has_active_children_impl",
    "workflow_sync_only_impl",
]
