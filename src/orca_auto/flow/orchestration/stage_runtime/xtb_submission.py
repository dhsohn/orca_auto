from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.flow.orchestration.services import OrchestrationServices
from orca_auto.flow.orchestration.stage_runtime.shared import (
    _apply_submission_result,
    _submission_is_deferred,
)
from orca_auto.flow.orchestration.stage_runtime.xtb_path_jobs import ensure_xtb_job_dir_impl
from orca_auto.flow.orchestration.stage_runtime.xtb_retry import (
    xtb_attempt_record_impl,
    xtb_current_attempt_number_impl,
)
from orca_auto.flow.orchestration.stage_views import WorkflowTaskView


def _record_xtb_submission_attempt(
    stage: dict[str, Any],
    submission: dict[str, Any],
    *,
    attempt_number: int,
    trigger_reason: str = "",
    trigger_message: str = "",
) -> None:
    attempt_record = xtb_attempt_record_impl(stage, attempt_number=attempt_number)
    attempt_record["submission_status"] = submission.get("status", "")
    attempt_record["submitted_at"] = submission.get("submitted_at", "")
    attempt_record["queue_id"] = submission.get("queue_id", "")
    if trigger_reason:
        attempt_record["trigger_reason"] = trigger_reason
    if trigger_message:
        attempt_record["trigger_message"] = trigger_message


def _apply_xtb_submission_result(
    stage: dict[str, Any],
    task: dict[str, Any],
    stage_metadata: dict[str, Any],
    submission: dict[str, Any],
    *,
    deferred_handoff_status: str,
    active_handoff_status: str,
) -> None:
    _apply_submission_result(
        stage=stage,
        task=task,
        stage_metadata=stage_metadata,
        submission=submission,
        deferred_metadata={"xtb_handoff_status": deferred_handoff_status},
        active_metadata={"xtb_handoff_status": active_handoff_status},
        metadata_fields=(("queue_id", "queue_id"),),
    )


def _submit_xtb_stage(
    services: OrchestrationServices,
    stage: dict[str, Any],
    task: dict[str, Any],
    stage_metadata: dict[str, Any],
    *,
    xtb_runtime_paths: dict[str, Path],
    xtb_config: str | None,
    workflow_id: str,
) -> bool:
    job_dir = ensure_xtb_job_dir_impl(
        stage,
        xtb_allowed_root=xtb_runtime_paths["allowed_root"],
        workflow_id=workflow_id,
    )
    submission = services.engines.submit_xtb_job_dir(
        job_dir=job_dir,
        priority=normalize_queue_priority(task["enqueue_payload"].get("priority")),
        config_path=str(xtb_config),
    )
    submission["submitted_at"] = services.clock.now_utc_iso()
    WorkflowTaskView(task).set_submission_result(submission)
    current_attempt = xtb_current_attempt_number_impl(stage)
    _record_xtb_submission_attempt(stage, submission, attempt_number=current_attempt)
    deferred = _submission_is_deferred(submission)
    _apply_xtb_submission_result(
        stage,
        task,
        stage_metadata,
        submission,
        deferred_handoff_status="waiting_for_slot",
        active_handoff_status="submitted",
    )
    if not deferred:
        stage_metadata["child_job_id"] = submission.get("job_id", "")
    return not deferred
