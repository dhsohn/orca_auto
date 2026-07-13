from __future__ import annotations

from typing import Any

from orca_auto.core.queue.publication import QUEUE_SUBMISSION_INTENT_KEY
from orca_auto.flow.orchestration.stage_views import WorkflowStageView, WorkflowTaskView

from . import stages as _restart_stages

_RESTARTABLE_STAGE_STATUSES = frozenset(
    {
        "failed",
        "cancelled",
        "cancel_failed",
        "submission_failed",
    }
)
_ACTIVE_STAGE_STATUSES = frozenset(
    {
        "queued",
        "running",
        "submitted",
        "cancel_requested",
    }
)
_STALE_STAGE_METADATA_KEYS = frozenset(
    {
        "analyzer_status",
        "cancel_requested",
        "child_job_id",
        "completed_at",
        "latest_known_path",
        "orca_attempts",
        "orca_current_attempt_number",
        "orca_final_result",
        "orca_latest_attempt_number",
        "orca_latest_attempt_status",
        "optimized_xyz_path",
        "queue_id",
        "queue_status",
        "reason",
        "run_id",
        QUEUE_SUBMISSION_INTENT_KEY,
        "state_status",
        "submission_status",
        "submitted_at",
    }
)
_STALE_TASK_PAYLOAD_KEYS = frozenset(
    {
        "last_out_path",
        "optimized_xyz_path",
        "orca_latest_attempt_inp",
        "orca_latest_attempt_out",
    }
)
_REMATERIALIZED_ENGINES = frozenset({"crest", "xtb"})
_REMATERIALIZED_TASK_PAYLOAD_KEYS = frozenset(
    {"job_dir", "selected_input_xyz", "secondary_input_xyz"}
)


_RESTART_STAGE_CONTEXT = _restart_stages.RestartStageContext(
    active_stage_statuses=_ACTIVE_STAGE_STATUSES,
    rematerialized_engines=_REMATERIALIZED_ENGINES,
    rematerialized_task_payload_keys=_REMATERIALIZED_TASK_PAYLOAD_KEYS,
    restartable_stage_statuses=_RESTARTABLE_STAGE_STATUSES,
    stale_stage_metadata_keys=_STALE_STAGE_METADATA_KEYS,
    stale_task_payload_keys=_STALE_TASK_PAYLOAD_KEYS,
)


def _stage_task(stage: dict[str, Any]) -> dict[str, Any]:
    return WorkflowStageView(stage).ensure_task().raw


def _stage_metadata(stage: dict[str, Any]) -> dict[str, Any]:
    return WorkflowStageView(stage).metadata(None)


def _task_metadata(task: dict[str, Any]) -> dict[str, Any]:
    return WorkflowTaskView(task).metadata(None)


def _task_payload(task: dict[str, Any]) -> dict[str, Any]:
    return WorkflowTaskView(task).payload(None)


def _task_engine(task: dict[str, Any]) -> str:
    return _RESTART_STAGE_CONTEXT.task_engine(WorkflowTaskView(task))


def _stage_needs_restart(stage: dict[str, Any]) -> bool:
    return _restart_stages.stage_needs_restart(stage, context=_RESTART_STAGE_CONTEXT)


def _active_stage_rows(payload: dict[str, Any]) -> list[dict[str, str]]:
    return _restart_stages.active_stage_rows(payload, context=_RESTART_STAGE_CONTEXT)


def _active_restart_error(workflow_id: str, rows: list[dict[str, str]]) -> ValueError:
    return _restart_stages.active_restart_error(workflow_id, rows)


def _clear_phase_notification_state(
    metadata: dict[str, Any], restarted_stages: list[dict[str, str]]
) -> None:
    _restart_stages.clear_phase_notification_state(
        metadata,
        restarted_stages,
        context=_RESTART_STAGE_CONTEXT,
    )


def _reset_stage_for_restart(
    stage: dict[str, Any],
    *,
    rematerialize: bool = False,
) -> dict[str, str]:
    return _restart_stages.reset_stage_for_restart(
        stage,
        rematerialize=rematerialize,
        context=_RESTART_STAGE_CONTEXT,
    )
