from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from orca_auto.core.statuses import (
    WORKFLOW_ACTIVE_STATUSES,
    WORKFLOW_ATTENTION_STATUSES,
    WORKFLOW_FAILED_STATUSES,
    WORKFLOW_STATUS_ORDER,
    WORKFLOW_TERMINAL_STATUSES,
    is_workflow_active_status,
    is_workflow_terminal_status,
    normalize_status,
)


def normalize_workflow_status(value: Any) -> str:
    return normalize_status(value)


def workflow_status_is_active(value: Any) -> bool:
    return is_workflow_active_status(value)


def workflow_status_needs_attention(value: Any) -> bool:
    return normalize_workflow_status(value) in WORKFLOW_ATTENTION_STATUSES


def workflow_status_is_terminal(value: Any) -> bool:
    return is_workflow_terminal_status(value)


def workflow_stage_is_terminal(stage_summary: dict[str, Any]) -> bool:
    stage_status = stage_summary.get("status")
    task_status = normalize_workflow_status(stage_summary.get("task_status"))
    if task_status in {"", "unknown"}:
        return workflow_status_is_terminal(stage_status)
    return workflow_status_is_terminal(stage_status) and workflow_status_is_terminal(task_status)


def select_current_stage(stage_summaries: Iterable[Any]) -> dict[str, Any]:
    stages = [stage for stage in stage_summaries if isinstance(stage, dict)]
    if not stages:
        return {}

    for stage in stages:
        if not workflow_stage_is_terminal(stage):
            return dict(stage)
    return dict(stages[-1])


__all__ = [
    "WORKFLOW_ACTIVE_STATUSES",
    "WORKFLOW_ATTENTION_STATUSES",
    "WORKFLOW_FAILED_STATUSES",
    "WORKFLOW_STATUS_ORDER",
    "WORKFLOW_TERMINAL_STATUSES",
    "normalize_workflow_status",
    "select_current_stage",
    "workflow_status_is_active",
    "workflow_status_is_terminal",
    "workflow_status_needs_attention",
    "workflow_stage_is_terminal",
]
