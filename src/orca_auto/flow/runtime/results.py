from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.statuses import (
    STATUS_CANCEL_FAILED,
    STATUS_CANCEL_REQUESTED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUBMITTED,
)

from . import _common as _runtime_common
from .models import WorkflowAdvanceResult

TERMINAL_WORKFLOW_STATUSES = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_CANCELLED,
        STATUS_CANCEL_FAILED,
    }
)
ACTIVE_TERMINAL_SYNC_STATUSES = frozenset(
    {STATUS_QUEUED, STATUS_RUNNING, STATUS_SUBMITTED, STATUS_CANCEL_REQUESTED}
)


def si_publish_retry_due(metadata: dict[str, Any]) -> bool:
    """Whether a pending SI publication should be retried this cycle."""

    if bool(metadata.get("si_publish_blocked")):
        return False
    return bool(metadata.get("si_publish_pending"))


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
) -> WorkflowAdvanceResult:
    result: WorkflowAdvanceResult = {
        "workflow_id": _runtime_common.normalize_text(payload.get("workflow_id"))
        or record.workflow_id,
        "template_name": _runtime_common.normalize_text(payload.get("template_name"))
        or record.template_name,
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
) -> bool:
    try:
        payload = load_workflow_payload_fn(workspace_dir)
    except (FileNotFoundError, ValueError):
        return False
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and bool(metadata.get("final_child_sync_pending")):
        return True
    if isinstance(metadata, dict) and si_publish_retry_due(metadata):
        return True
    for raw_stage in payload.get("stages", []):
        if not isinstance(raw_stage, dict):
            continue
        if (
            _runtime_common.normalize_text(raw_stage.get("status")).lower()
            in ACTIVE_TERMINAL_SYNC_STATUSES
        ):
            return True
        task = raw_stage.get("task")
        if (
            isinstance(task, dict)
            and _runtime_common.normalize_text(task.get("status")).lower()
            in ACTIVE_TERMINAL_SYNC_STATUSES
        ):
            return True
    return workflow_has_active_downstream_fn(payload)


__all__ = [
    "ACTIVE_TERMINAL_SYNC_STATUSES",
    "TERMINAL_WORKFLOW_STATUSES",
    "si_publish_retry_due",
    "workflow_advance_failed_result",
    "workflow_advanced_result",
    "workflow_needs_terminal_sync",
    "workflow_skipped_terminal_result",
]
