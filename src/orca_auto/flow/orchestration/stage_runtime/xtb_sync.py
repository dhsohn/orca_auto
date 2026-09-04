from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.statuses import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PLANNED,
    SUBMISSION_DEFERRED_STATUSES,
    status_in,
)
from orca_auto.core.utils import normalize_text, safe_int
from orca_auto.flow.orchestration.services import OrchestrationServices
from orca_auto.flow.orchestration.stage_runtime.shared import (
    EngineStageSyncContext,
    _apply_contract_status,
    _apply_submission_result,
    _engine_job_dir_contract_lookup,
    _engine_stage_sync_context,
    _load_contract_or_none,
    _submission_is_deferred,
)
from orca_auto.flow.orchestration.stage_runtime.xtb_handoff import (
    _empty_xtb_handoff,
    xtb_handoff_status_impl,
)
from orca_auto.flow.orchestration.stage_runtime.xtb_path_jobs import (
    ensure_xtb_job_dir_impl,
    write_xtb_path_job_impl,
)
from orca_auto.flow.orchestration.stage_runtime.xtb_retry import (
    xtb_attempt_record_impl,
    xtb_current_attempt_number_impl,
    xtb_path_retry_limit_impl,
)
from orca_auto.flow.orchestration.stage_views import WorkflowStageView, WorkflowTaskView
from orca_auto.flow.state import workflow_workspace_internal_engine_paths


@dataclass(frozen=True)
class _XtbHandoffRetryDecision:
    should_retry: bool
    retries_used: int
    retry_limit: int
    next_attempt: int


def _load_xtb_contract(
    services: OrchestrationServices,
    stage: dict[str, Any],
    task_payload: dict[str, Any],
    *,
    xtb_runtime_paths: dict[str, Path],
    xtb_config: str | None,
) -> Any | None:
    lookup = _engine_job_dir_contract_lookup(
        services,
        stage,
        task_payload,
        runtime_paths=xtb_runtime_paths,
        config_path=xtb_config,
        engine="xtb",
    )
    if lookup is None:
        return None
    target, index_root = lookup
    return _load_contract_or_none(
        services.engines.load_xtb_artifact_contract,
        engine="xtb",
        target=target,
        stage=stage,
        xtb_index_root=index_root,
    )


def _apply_xtb_contract(
    services: OrchestrationServices,
    stage_view: WorkflowStageView,
    task_view: WorkflowTaskView,
    contract: Any,
) -> dict[str, str] | None:
    stage = stage_view.raw
    task = task_view.raw
    if not _apply_contract_status(stage, task, contract.status):
        return None
    stage_view.update_xtb_contract_metadata(contract)
    task_view.set_selected_input_xyz(contract.selected_input_xyz)

    current_attempt = stage_view.xtb_current_attempt_number()
    handoff = (
        xtb_handoff_status_impl(contract, services=services)
        if task_view.kind() == "path_search"
        else _empty_xtb_handoff()
    )
    stage_view.update_xtb_attempt_record(
        current_attempt,
        {
            "job_id": contract.job_id,
            "status": contract.status,
            "reason": contract.reason,
            "latest_known_path": contract.latest_known_path,
            "candidate_count": len(contract.candidate_details),
            "selected_candidate_paths": list(contract.selected_candidate_paths),
            "analysis_summary": dict(contract.analysis_summary),
            "handoff_status": handoff["status"],
            "handoff_reason": handoff["reason"],
            "handoff_message": handoff["message"],
            "completed_at": normalize_text(contract.analysis_summary.get("completed_at")),
        },
    )
    stage_view.set_reaction_handoff(handoff)
    return handoff


def _xtb_handoff_retry_candidate(
    stage_view: WorkflowStageView,
    task_view: WorkflowTaskView,
    handoff: dict[str, str],
    *,
    xtb_config: str | None,
    submit_ready: bool,
) -> bool:
    return (
        submit_ready
        and bool(normalize_text(xtb_config))
        and task_view.kind() == "path_search"
        and handoff["status"] == STATUS_FAILED
        and status_in(stage_view.raw.get("status"), {STATUS_COMPLETED, STATUS_FAILED})
    )


def _xtb_handoff_retry_budget(
    stage: dict[str, Any],
    stage_metadata: dict[str, Any],
) -> tuple[int, int]:
    retries_used = safe_int(stage_metadata.get("xtb_handoff_retries_used"), default=0)
    retry_limit = xtb_path_retry_limit_impl(stage)
    return retries_used, retry_limit


def _xtb_handoff_retry_decision(
    stage_view: WorkflowStageView,
    task_view: WorkflowTaskView,
    handoff: dict[str, str],
    *,
    xtb_config: str | None,
    submit_ready: bool,
) -> _XtbHandoffRetryDecision:
    if not _xtb_handoff_retry_candidate(
        stage_view,
        task_view,
        handoff,
        xtb_config=xtb_config,
        submit_ready=submit_ready,
    ):
        return _XtbHandoffRetryDecision(False, 0, 0, 0)

    retries_used, retry_limit = _xtb_handoff_retry_budget(
        stage_view.raw,
        stage_view.metadata(),
    )
    return _XtbHandoffRetryDecision(
        retries_used < retry_limit,
        retries_used,
        retry_limit,
        retries_used + 1,
    )


def _pending_xtb_handoff_retry_decision(
    stage_view: WorkflowStageView,
    task_view: WorkflowTaskView,
) -> _XtbHandoffRetryDecision | None:
    stage_metadata = stage_view.metadata()
    if (
        task_view.kind() != "path_search"
        or task_view.status() != STATUS_PLANNED
        or normalize_text(stage_metadata.get("reaction_handoff_status")) != "retrying"
        or normalize_text(stage_metadata.get("xtb_handoff_status")) != "waiting_for_slot"
    ):
        return None
    attempt_number = stage_view.xtb_current_attempt_number()
    if attempt_number <= 0:
        return None
    attempt_record = xtb_attempt_record_impl(
        stage_view.raw,
        attempt_number=attempt_number,
    )
    materialized_job_dir = normalize_text(task_view.payload().get("job_dir"))
    if (
        not materialized_job_dir
        or materialized_job_dir != normalize_text(attempt_record.get("job_dir"))
        or not status_in(
            attempt_record.get("submission_status"),
            SUBMISSION_DEFERRED_STATUSES,
        )
    ):
        return None
    retries_used = safe_int(stage_metadata.get("xtb_handoff_retries_used"), default=0)
    retry_limit = safe_int(
        stage_metadata.get("xtb_handoff_retry_limit"),
        default=xtb_path_retry_limit_impl(stage_view.raw),
    )
    if attempt_number != retries_used + 1 or attempt_number > retry_limit:
        return None
    return _XtbHandoffRetryDecision(
        should_retry=True,
        retries_used=retries_used,
        retry_limit=retry_limit,
        next_attempt=attempt_number,
    )


def _submit_xtb_handoff_retry(
    services: OrchestrationServices,
    stage_view: WorkflowStageView,
    task_view: WorkflowTaskView,
    handoff: dict[str, str],
    *,
    xtb_runtime_paths: dict[str, Path],
    xtb_config: str | None,
    workflow_id: str,
    attempt_number: int,
) -> dict[str, Any]:
    stage = stage_view.raw
    task = task_view.raw
    retry_job_dir = write_xtb_path_job_impl(
        stage,
        xtb_allowed_root=xtb_runtime_paths["allowed_root"],
        workflow_id=workflow_id,
        attempt_number=attempt_number,
    )
    task_view.update_payload({"job_dir": retry_job_dir})
    task_view.update_enqueue_payload({"job_dir": retry_job_dir})
    submission = services.engines.submit_xtb_job_dir(
        job_dir=retry_job_dir,
        priority=normalize_queue_priority(task["enqueue_payload"].get("priority")),
        config_path=str(xtb_config),
    )
    submission["submitted_at"] = services.clock.now_utc_iso()
    task_view.set_submission_result(submission)
    _record_xtb_submission_attempt(
        stage,
        submission,
        attempt_number=attempt_number,
        trigger_reason=handoff["reason"],
        trigger_message=handoff["message"],
    )
    return submission


def _apply_xtb_handoff_retry_submission(
    stage_view: WorkflowStageView,
    task_view: WorkflowTaskView,
    stage_metadata: dict[str, Any],
    submission: dict[str, Any],
    decision: _XtbHandoffRetryDecision,
) -> bool:
    submission_applied = _apply_xtb_submission_result(
        stage_view.raw,
        task_view.raw,
        stage_metadata,
        submission,
        deferred_handoff_status="waiting_for_slot",
        active_handoff_status="retrying",
    )
    job_dir = str(task_view.payload().get("job_dir") or "").strip()
    if job_dir:
        stage_metadata["latest_known_path"] = job_dir
    job_id = str(submission.get("job_id") or "").strip()
    deferred = _submission_is_deferred(submission)
    if job_id and (submission_applied or deferred):
        stage_metadata["child_job_id"] = job_id
    else:
        stage_metadata.pop("child_job_id", None)
    if not submission_applied and not deferred:
        stage_metadata["reaction_handoff_status"] = STATUS_FAILED
        return False
    if submission_applied:
        stage_view.set_xtb_handoff_retrying(
            retry_limit=decision.retry_limit,
            retries_used=decision.next_attempt,
        )
    else:
        stage_view.set_xtb_handoff_retrying(retry_limit=decision.retry_limit)
    return submission_applied


def _maybe_retry_xtb_handoff(
    services: OrchestrationServices,
    stage_view: WorkflowStageView,
    task_view: WorkflowTaskView,
    handoff: dict[str, str],
    *,
    xtb_runtime_paths: dict[str, Path],
    xtb_config: str | None,
    submit_ready: bool,
    workflow_id: str,
) -> bool:
    stage_metadata = stage_view.metadata()
    decision = _xtb_handoff_retry_decision(
        stage_view,
        task_view,
        handoff,
        xtb_config=xtb_config,
        submit_ready=submit_ready,
    )
    if not decision.should_retry:
        return False

    submission = _submit_xtb_handoff_retry(
        services,
        stage_view,
        task_view,
        handoff,
        xtb_runtime_paths=xtb_runtime_paths,
        xtb_config=xtb_config,
        workflow_id=workflow_id,
        attempt_number=decision.next_attempt,
    )
    _apply_xtb_handoff_retry_submission(
        stage_view,
        task_view,
        stage_metadata,
        submission,
        decision,
    )
    return True


def _xtb_output_artifacts(contract: Any) -> list[dict[str, Any]]:
    return [
        {
            "kind": "xtb_candidate",
            "path": item.path,
            "selected": item.selected,
            "metadata": {
                "rank": item.rank,
                "kind": item.kind,
                "score": item.score,
                **dict(item.metadata),
            },
        }
        for item in contract.candidate_details
    ]


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
) -> bool:
    submission_applied = _apply_submission_result(
        stage=stage,
        task=task,
        stage_metadata=stage_metadata,
        submission=submission,
        deferred_metadata={"xtb_handoff_status": deferred_handoff_status},
        active_metadata={"xtb_handoff_status": active_handoff_status},
        metadata_fields=(("queue_id", "queue_id"),),
    )
    if not submission_applied and not _submission_is_deferred(submission):
        stage_metadata.pop("xtb_handoff_status", None)
    return submission_applied


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
    stage_view = WorkflowStageView(stage)
    task_view = WorkflowTaskView(task)
    pending_retry = _pending_xtb_handoff_retry_decision(stage_view, task_view)
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
    task_view.set_submission_result(submission)
    current_attempt = xtb_current_attempt_number_impl(stage)
    _record_xtb_submission_attempt(
        stage,
        submission,
        attempt_number=current_attempt,
    )
    if pending_retry is not None:
        return _apply_xtb_handoff_retry_submission(
            stage_view,
            task_view,
            stage_metadata,
            submission,
            pending_retry,
        )
    submission_applied = _apply_xtb_submission_result(
        stage,
        task,
        stage_metadata,
        submission,
        deferred_handoff_status="waiting_for_slot",
        active_handoff_status="submitted",
    )
    if submission_applied:
        stage_metadata["child_job_id"] = submission.get("job_id", "")
    elif not _submission_is_deferred(submission):
        stage_metadata.pop("child_job_id", None)
    return submission_applied


def _submit_xtb_stage_if_needed(
    context: EngineStageSyncContext,
    *,
    xtb_runtime_paths: dict[str, Path],
    xtb_config: str | None,
    submit_ready: bool,
    workflow_id: str,
) -> bool:
    if not context.should_submit(submit_ready=submit_ready, config_path=xtb_config):
        return True
    return _submit_xtb_stage(
        context.services,
        context.stage,
        context.task,
        context.stage_metadata,
        xtb_runtime_paths=xtb_runtime_paths,
        xtb_config=xtb_config,
        workflow_id=workflow_id,
    )


def _load_and_apply_xtb_contract(
    context: EngineStageSyncContext,
    *,
    xtb_runtime_paths: dict[str, Path],
    xtb_config: str | None,
) -> tuple[Any, dict[str, str]] | None:
    contract = _load_xtb_contract(
        context.services,
        context.stage,
        context.task_payload,
        xtb_runtime_paths=xtb_runtime_paths,
        xtb_config=xtb_config,
    )
    if contract is None:
        return None
    handoff = _apply_xtb_contract(
        context.services,
        context.stage_view,
        context.task_view,
        contract,
    )
    if handoff is None:
        return None
    return contract, handoff


def _materialize_xtb_stage_result(
    context: EngineStageSyncContext,
    contract: Any,
) -> None:
    retries_used, retry_limit = _xtb_handoff_retry_budget(
        context.stage,
        context.stage_metadata,
    )
    context.stage_view.set_xtb_handoff_retry_state(
        retries_used=retries_used,
        retry_limit=retry_limit,
    )
    context.set_output_artifacts(_xtb_output_artifacts(contract))


def sync_xtb_stage_impl(
    stage: dict[str, Any],
    *,
    xtb_config: str | None,
    submit_ready: bool,
    workflow_id: str,
    workspace_dir: Path,
    services: OrchestrationServices | None = None,
) -> None:
    context = _engine_stage_sync_context(stage, engine="xtb", services=services)
    if context is None:
        return
    xtb_runtime_paths = workflow_workspace_internal_engine_paths(workspace_dir, engine="xtb")
    submission_applied = _submit_xtb_stage_if_needed(
        context,
        xtb_runtime_paths=xtb_runtime_paths,
        xtb_config=xtb_config,
        submit_ready=submit_ready,
        workflow_id=workflow_id,
    )
    if not submission_applied:
        # As with CREST, applying the old active contract here would replace
        # the new deferred or rejected submission state in this same tick.
        return
    contract_result = _load_and_apply_xtb_contract(
        context,
        xtb_runtime_paths=xtb_runtime_paths,
        xtb_config=xtb_config,
    )
    if contract_result is None:
        return
    contract, handoff = contract_result
    if _maybe_retry_xtb_handoff(
        context.services,
        context.stage_view,
        context.task_view,
        handoff,
        xtb_runtime_paths=xtb_runtime_paths,
        xtb_config=xtb_config,
        submit_ready=submit_ready,
        workflow_id=workflow_id,
    ):
        return
    _materialize_xtb_stage_result(context, contract)


__all__ = [
    "sync_xtb_stage_impl",
]
