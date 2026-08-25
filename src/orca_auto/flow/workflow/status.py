from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from orca_auto.core.statuses import is_workflow_terminal_status, normalize_status


def workflow_stage_is_terminal(stage_summary: dict[str, Any]) -> bool:
    stage_status = stage_summary.get("status")
    task_status = normalize_status(stage_summary.get("task_status"))
    if task_status in {"", "unknown"}:
        return is_workflow_terminal_status(stage_status)
    return is_workflow_terminal_status(stage_status) and is_workflow_terminal_status(task_status)


def select_current_stage(stage_summaries: Iterable[Any]) -> dict[str, Any]:
    stages = [stage for stage in stage_summaries if isinstance(stage, dict)]
    if not stages:
        return {}

    for stage in stages:
        if not workflow_stage_is_terminal(stage):
            return dict(stage)
    return dict(stages[-1])


__all__ = [
    "select_current_stage",
    "workflow_stage_is_terminal",
]
