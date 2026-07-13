from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.statuses import STATUS_COMPLETED, STATUS_FAILED, status_in
from orca_auto.flow.orchestration.dep_types import OrchestrationDeps
from orca_auto.flow.orchestration.stage_runtime.shared import (
    EngineStageSyncContext,
    _apply_contract_status,
    _engine_job_dir_contract_lookup,
    _engine_stage_sync_context,
    _load_contract_or_none,
    _submission_is_deferred,
)
from orca_auto.flow.orchestration.stage_runtime.xtb_handoff import (
    _empty_xtb_handoff,
)
from orca_auto.flow.orchestration.stage_runtime.xtb_submission import (
    _apply_xtb_submission_result,
    _record_xtb_submission_attempt,
    _submit_xtb_stage,
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
    o: Any,
    stage: dict[str, Any],
    task_payload: dict[str, Any],
    *,
    xtb_runtime_paths: dict[str, Path],
    xtb_config: str | None,
) -> Any | None:
    lookup = _engine_job_dir_contract_lookup(
        o,
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
        o.engines.load_xtb_artifact_contract,
        engine="xtb",
        target=target,
        stage=stage,
        xtb_index_root=index_root,
    )


def _apply_xtb_contract(
    o: Any,
    stage_view: WorkflowStageView,
    task_view: WorkflowTaskView,
    contract: Any,
) -> dict[str, str]:
    stage = stage_view.raw
    task = task_view.raw
    _apply_contract_status(stage, task, contract.status)
    stage_view.update_xtb_contract_metadata(contract)
    task_view.set_selected_input_xyz(contract.selected_input_xyz)

    current_attempt = stage_view.xtb_current_attempt_number()
    handoff = (
        o.stages.runtime._xtb_handoff_status(contract)
        if task_view.kind(o) == "path_search"
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
            "completed_at": o.stages.support._normalize_text(
                contract.analysis_summary.get("completed_at")
            ),
        },
    )
    stage_view.set_reaction_handoff(handoff)
    return handoff


def _xtb_handoff_retry_candidate(
    o: Any,
    stage_view: WorkflowStageView,
    task_view: WorkflowTaskView,
    handoff: dict[str, str],
    *,
    xtb_config: str | None,
    submit_ready: bool,
) -> bool:
    return (
        submit_ready
        and bool(o.stages.support._normalize_text(xtb_config))
        and task_view.kind(o) == "path_search"
        and handoff["status"] == STATUS_FAILED
        and status_in(stage_view.raw.get("status"), {STATUS_COMPLETED, STATUS_FAILED})
    )


def _xtb_handoff_retry_budget(
    o: Any,
    stage: dict[str, Any],
    stage_metadata: dict[str, Any],
) -> tuple[int, int]:
    retries_used = o.stages.support._safe_int(
        stage_metadata.get("xtb_handoff_retries_used"), default=0
    )
    retry_limit = o.stages.runtime._xtb_path_retry_limit(stage)
    return retries_used, retry_limit


def _xtb_handoff_retry_decision(
    o: Any,
    stage_view: WorkflowStageView,
    task_view: WorkflowTaskView,
    handoff: dict[str, str],
    *,
    xtb_config: str | None,
    submit_ready: bool,
) -> _XtbHandoffRetryDecision:
    if not _xtb_handoff_retry_candidate(
        o,
        stage_view,
        task_view,
        handoff,
        xtb_config=xtb_config,
        submit_ready=submit_ready,
    ):
        return _XtbHandoffRetryDecision(False, 0, 0, 0)

    retries_used, retry_limit = _xtb_handoff_retry_budget(
        o,
        stage_view.raw,
        stage_view.metadata(None),
    )
    return _XtbHandoffRetryDecision(
        retries_used < retry_limit,
        retries_used,
        retry_limit,
        retries_used + 1,
    )


def _submit_xtb_handoff_retry(
    o: Any,
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
    retry_job_dir = o.stages.runtime._write_xtb_path_job(
        stage,
        xtb_allowed_root=xtb_runtime_paths["allowed_root"],
        workflow_id=workflow_id,
        attempt_number=attempt_number,
    )
    task_view.update_payload({"job_dir": retry_job_dir})
    task_view.update_enqueue_payload({"job_dir": retry_job_dir})
    submission = o.engines.submit_xtb_job_dir(
        job_dir=retry_job_dir,
        priority=normalize_queue_priority(task["enqueue_payload"].get("priority")),
        config_path=str(xtb_config),
    )
    submission["submitted_at"] = o.persistence.now_utc_iso()
    task_view.set_submission_result(submission)
    _record_xtb_submission_attempt(
        o,
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
) -> None:
    _apply_xtb_submission_result(
        stage_view.raw,
        task_view.raw,
        stage_metadata,
        submission,
        deferred_handoff_status="waiting_for_slot",
        active_handoff_status="retrying",
    )
    job_dir = str(task_view.payload(None).get("job_dir") or "").strip()
    if job_dir:
        stage_metadata["latest_known_path"] = job_dir
    job_id = str(submission.get("job_id") or "").strip()
    if job_id:
        stage_metadata["child_job_id"] = job_id
    else:
        stage_metadata.pop("child_job_id", None)
    if not _submission_is_deferred(submission):
        stage_view.set_xtb_handoff_retrying(
            retry_limit=decision.retry_limit,
            retries_used=decision.next_attempt,
        )
    else:
        stage_view.set_xtb_handoff_retrying(retry_limit=decision.retry_limit)


def _maybe_retry_xtb_handoff(
    o: Any,
    stage_view: WorkflowStageView,
    task_view: WorkflowTaskView,
    handoff: dict[str, str],
    *,
    xtb_runtime_paths: dict[str, Path],
    xtb_config: str | None,
    submit_ready: bool,
    workflow_id: str,
) -> bool:
    stage_metadata = stage_view.metadata(None)
    decision = _xtb_handoff_retry_decision(
        o,
        stage_view,
        task_view,
        handoff,
        xtb_config=xtb_config,
        submit_ready=submit_ready,
    )
    if not decision.should_retry:
        return False

    submission = _submit_xtb_handoff_retry(
        o,
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
        context.o,
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
        context.o,
        context.stage,
        context.task_payload,
        xtb_runtime_paths=xtb_runtime_paths,
        xtb_config=xtb_config,
    )
    if contract is None:
        return None
    handoff = _apply_xtb_contract(
        context.o,
        context.stage_view,
        context.task_view,
        contract,
    )
    return contract, handoff


def _materialize_xtb_stage_result(
    context: EngineStageSyncContext,
    contract: Any,
) -> None:
    retries_used, retry_limit = _xtb_handoff_retry_budget(
        context.o,
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
    deps: OrchestrationDeps | None = None,
) -> None:
    context = _engine_stage_sync_context(stage, engine="xtb", deps=deps)
    if context is None:
        return
    o = context.o
    xtb_runtime_paths = workflow_workspace_internal_engine_paths(workspace_dir, engine="xtb")
    submission_applied = _submit_xtb_stage_if_needed(
        context,
        xtb_runtime_paths=xtb_runtime_paths,
        xtb_config=xtb_config,
        submit_ready=submit_ready,
        workflow_id=workflow_id,
    )
    if not submission_applied:
        # As with CREST, applying the old active contract here would replace the
        # deferred PLANNED state and prevent the next-tick retry.
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
        o,
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
