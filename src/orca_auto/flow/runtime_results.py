from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import _runtime_common
from .runtime_models import WorkflowAdvanceResult

TERMINAL_WORKFLOW_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "cancel_failed",
    }
)
ACTIVE_TERMINAL_SYNC_STATUSES = frozenset({"queued", "running", "submitted", "cancel_requested"})


def workflow_advance_failed_result(
    record: Any, *, previous_status: str, reason: str
) -> WorkflowAdvanceResult:
    return {
        "workflow_id": record.workflow_id,
        "template_name": record.template_name,
        "previous_status": previous_status,
        "status": "advance_failed",
        "advanced": False,
        "reason": reason,
        "stage_count": record.stage_count,
    }


def workflow_skipped_terminal_result(record: Any, *, previous_status: str) -> WorkflowAdvanceResult:
    return {
        "workflow_id": record.workflow_id,
        "template_name": record.template_name,
        "previous_status": previous_status,
        "status": previous_status,
        "advanced": False,
        "reason": "terminal_status",
        "stage_count": record.stage_count,
    }


def workflow_advanced_result(
    record: Any,
    payload: dict[str, Any],
    *,
    previous_status: str,
    status: str,
    reason: str = "",
    normalize_text_fn: Callable[[Any], str] = _runtime_common.normalize_text,
) -> WorkflowAdvanceResult:
    result: WorkflowAdvanceResult = {
        "workflow_id": normalize_text_fn(payload.get("workflow_id")) or record.workflow_id,
        "template_name": normalize_text_fn(payload.get("template_name")) or record.template_name,
        "previous_status": previous_status,
        "status": status,
        "advanced": True,
        "changed": status != previous_status,
        "stage_count": len(payload.get("stages", []))
        if isinstance(payload.get("stages"), list)
        else record.stage_count,
    }
    if reason:
        result["reason"] = reason
    return result


def workflow_needs_terminal_sync(
    workspace_dir: str | Path,
    *,
    load_workflow_payload_fn: Callable[[str | Path], dict[str, Any]],
    workflow_has_active_downstream_fn: Callable[[dict[str, Any]], bool],
    normalize_text_fn: Callable[[Any], str] = _runtime_common.normalize_text,
) -> bool:
    try:
        payload = load_workflow_payload_fn(workspace_dir)
    except (FileNotFoundError, ValueError):
        return False
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and bool(metadata.get("final_child_sync_pending")):
        return True
    for raw_stage in payload.get("stages", []):
        if not isinstance(raw_stage, dict):
            continue
        if normalize_text_fn(raw_stage.get("status")).lower() in ACTIVE_TERMINAL_SYNC_STATUSES:
            return True
        task = raw_stage.get("task")
        if (
            isinstance(task, dict)
            and normalize_text_fn(task.get("status")).lower() in ACTIVE_TERMINAL_SYNC_STATUSES
        ):
            return True
    return workflow_has_active_downstream_fn(payload)


__all__ = [
    "ACTIVE_TERMINAL_SYNC_STATUSES",
    "TERMINAL_WORKFLOW_STATUSES",
    "workflow_advance_failed_result",
    "workflow_advanced_result",
    "workflow_needs_terminal_sync",
    "workflow_skipped_terminal_result",
]
