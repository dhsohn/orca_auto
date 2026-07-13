from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.queue.publication import QUEUE_SUBMISSION_INTENT_KEY
from orca_auto.core.statuses import (
    CANCEL_ACK_STATUSES,
    STATUS_CANCEL_FAILED,
    STATUS_CANCEL_REQUESTED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_SUBMITTED,
)

from .orca_models import (
    CancelStageContext,
    SiblingSubmitterConfig,
    TaskRecordMutator,
    WorkflowBuckets,
    WorkflowStageOutcome,
    sibling_submitter_config,
    workflow_metadata,
)
from .orca_submission import orca_submitter_matches


@dataclass(frozen=True)
class CancellationDeps:
    normalize_text: Callable[[Any], str]
    now_utc_iso: Callable[[], str]
    resolve_workflow_workspace: Callable[..., Path]
    acquire_workflow_lock: Callable[[Path], AbstractContextManager[None]]
    load_workflow_payload: Callable[[Path], dict[str, Any]]
    write_workflow_payload: Callable[[Path, dict[str, Any]], Any]
    sync_workflow_registry: Callable[[str | Path, Path, dict[str, Any]], Any]
    cancel_target: Callable[..., dict[str, Any]]
    recover_exact_reaction_dir_submission: Callable[..., dict[str, Any] | None]


CANCEL_RESULT = TaskRecordMutator("cancel_result")
_CANCEL_SKIP_TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED})


def cancel_stage_context(
    stage: dict[str, Any], deps: CancellationDeps
) -> CancelStageContext | None:
    task = stage.get("task")
    if not isinstance(task, dict):
        return None
    metadata = task.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        task["metadata"] = metadata
    stage_metadata = stage.get("metadata")
    if not isinstance(stage_metadata, dict):
        stage_metadata = {}
        stage["metadata"] = stage_metadata
    enqueue_payload = task.get("enqueue_payload")
    if not isinstance(enqueue_payload, dict):
        enqueue_payload = {}

    payload = task.get("payload")
    payload_reaction_dir = payload.get("reaction_dir") if isinstance(payload, dict) else ""
    raw_force = enqueue_payload.get("force", False)
    force = (
        raw_force
        if isinstance(raw_force, bool)
        else str(raw_force).strip().lower() in {"1", "true", "yes", "on"}
    )
    return CancelStageContext(
        stage=stage,
        task=task,
        stage_metadata=stage_metadata,
        enqueue_payload=enqueue_payload,
        stage_id=deps.normalize_text(stage.get("stage_id")),
        task_status=deps.normalize_text(task.get("status")).lower(),
        stage_status=deps.normalize_text(stage.get("status")).lower(),
        queue_id=deps.normalize_text(stage_metadata.get("queue_id")),
        reaction_dir=deps.normalize_text(payload_reaction_dir)
        or deps.normalize_text(enqueue_payload.get("reaction_dir")),
        submission_intent_token=deps.normalize_text(
            stage_metadata.get(QUEUE_SUBMISSION_INTENT_KEY)
        ),
        priority=normalize_queue_priority(enqueue_payload.get("priority")),
        force=force,
    )


def cancel_skip_reason(context: CancelStageContext) -> str:
    if context.task_status in CANCEL_ACK_STATUSES or context.stage_status in CANCEL_ACK_STATUSES:
        return "already_cancelled"
    if (
        context.task_status in _CANCEL_SKIP_TERMINAL_STATUSES
        or context.stage_status in _CANCEL_SKIP_TERMINAL_STATUSES
    ):
        return "already_terminal"
    return ""


def record_cancel_skip(context: CancelStageContext, reason: str) -> WorkflowStageOutcome:
    return WorkflowStageOutcome(
        bucket="skipped",
        detail={"stage_id": context.stage_id, "reason": reason},
        stage_result={"stage_id": context.stage_id, "status": STATUS_SKIPPED, "reason": reason},
    )


def needs_orca_cancel(context: CancelStageContext) -> bool:
    return bool(
        context.queue_id
        or context.submission_intent_token
        or context.task_status in {STATUS_SUBMITTED}
        or context.stage_status in {STATUS_QUEUED, STATUS_RUNNING}
    )


def recover_intent_queue_id(
    context: CancelStageContext,
    *,
    submitter_config: SiblingSubmitterConfig,
    deps: CancellationDeps,
) -> WorkflowStageOutcome | None:
    """Recover the queue row committed before a workflow payload crash."""
    if context.queue_id or not context.submission_intent_token:
        return None
    try:
        recovered = deps.recover_exact_reaction_dir_submission(
            reaction_dir=context.reaction_dir,
            priority=context.priority,
            config_path=submitter_config.config_path,
            force=context.force,
            repo_root=submitter_config.repo_root,
            submission_intent_token=context.submission_intent_token,
            allow_unclaimable_active=True,
            raise_on_lookup_error=True,
        )
    except (OSError, ValueError):
        return record_cancel_failure(
            context,
            reason="submission_intent_lookup_failed",
            deps=deps,
        )
    if recovered is None:
        return None
    queue_id = deps.normalize_text(recovered.get("queue_id"))
    if not queue_id:
        reason = deps.normalize_text(recovered.get("reason")) or "ambiguous_submission_recovery"
        return record_cancel_failure(context, reason=reason, deps=deps)
    context.queue_id = queue_id
    context.stage_metadata["queue_id"] = queue_id
    return None


def apply_cancel_result(
    context: CancelStageContext,
    *,
    cancel_record: dict[str, Any],
    task_status: str | None = None,
    stage_status: str | None = None,
    metadata_updates: dict[str, Any] | None = None,
) -> None:
    CANCEL_RESULT.apply(
        stage=context.stage,
        task=context.task,
        stage_metadata=context.stage_metadata,
        task_record=cancel_record,
        task_status=task_status,
        stage_status=stage_status,
        metadata_updates=metadata_updates,
    )


def record_local_cancel(
    context: CancelStageContext, deps: CancellationDeps
) -> WorkflowStageOutcome:
    cancel_record = {
        "status": STATUS_CANCELLED,
        "cancelled_at": deps.now_utc_iso(),
        "mode": "local",
    }
    apply_cancel_result(
        context,
        cancel_record=cancel_record,
        task_status=STATUS_CANCELLED,
        stage_status=STATUS_CANCELLED,
        metadata_updates={
            "cancel_status": STATUS_CANCELLED,
            "cancelled_at": cancel_record["cancelled_at"],
        },
    )
    return WorkflowStageOutcome(
        bucket="cancelled",
        detail={"stage_id": context.stage_id, "mode": "local"},
        stage_result={"stage_id": context.stage_id, "status": STATUS_CANCELLED, "mode": "local"},
    )


def record_cancel_failure(
    context: CancelStageContext,
    *,
    reason: str,
    deps: CancellationDeps,
    stage_result: dict[str, Any] | None = None,
) -> WorkflowStageOutcome:
    cancel_record = {
        "status": STATUS_FAILED,
        "reason": reason,
        "cancelled_at": deps.now_utc_iso(),
    }
    apply_cancel_result(
        context,
        cancel_record=cancel_record,
    )
    return WorkflowStageOutcome(
        bucket="failed",
        detail={"stage_id": context.stage_id, "reason": reason},
        stage_result=stage_result
        or {"stage_id": context.stage_id, "status": STATUS_CANCEL_FAILED, "reason": reason},
    )


def record_remote_cancel_success(
    context: CancelStageContext,
    *,
    cancel_record: dict[str, Any],
    cancel_status: str,
) -> WorkflowStageOutcome:
    apply_cancel_result(
        context,
        cancel_record=cancel_record,
        task_status=cancel_status,
        stage_status=cancel_status,
        metadata_updates={
            "cancel_status": cancel_status,
            "cancelled_at": cancel_record["cancelled_at"],
        },
    )
    bucket = {STATUS_CANCEL_REQUESTED: "requested"}.get(cancel_status, "cancelled")
    return WorkflowStageOutcome(
        bucket=bucket,
        detail={
            "stage_id": context.stage_id,
            "queue_id": context.queue_id,
            "reaction_dir": context.reaction_dir,
        },
        stage_result={"stage_id": context.stage_id, "status": cancel_status},
    )


def record_remote_cancel_failed(
    context: CancelStageContext,
    cancel_record: dict[str, Any],
) -> WorkflowStageOutcome:
    returncode = int(cancel_record.get("returncode", 1))
    apply_cancel_result(
        context,
        cancel_record=cancel_record,
    )
    return WorkflowStageOutcome(
        bucket="failed",
        detail={
            "stage_id": context.stage_id,
            "queue_id": context.queue_id,
            "reaction_dir": context.reaction_dir,
            "returncode": returncode,
        },
        stage_result={
            "stage_id": context.stage_id,
            "status": STATUS_CANCEL_FAILED,
            "returncode": returncode,
        },
    )


def record_remote_cancel(
    context: CancelStageContext,
    *,
    cancel_identifier: str,
    submitter_config: SiblingSubmitterConfig,
    deps: CancellationDeps,
) -> WorkflowStageOutcome:
    cancel_record = deps.cancel_target(
        target=cancel_identifier,
        config_path=submitter_config.config_path,
        repo_root=submitter_config.repo_root,
    )
    cancel_status = str(cancel_record.get("status", STATUS_FAILED))
    cancel_record["cancelled_at"] = deps.now_utc_iso()
    cancel_record["target"] = cancel_identifier
    if cancel_status in {STATUS_CANCEL_REQUESTED, STATUS_CANCELLED}:
        return record_remote_cancel_success(
            context,
            cancel_record=cancel_record,
            cancel_status=cancel_status,
        )
    return record_remote_cancel_failed(context, cancel_record)


def remote_cancel_identifier(context: CancelStageContext) -> str:
    return context.queue_id or context.reaction_dir


def remote_cancel_preflight_failure(
    context: CancelStageContext,
    *,
    submitter_config: SiblingSubmitterConfig,
    deps: CancellationDeps,
) -> WorkflowStageOutcome | None:
    if not remote_cancel_identifier(context):
        return record_cancel_failure(context, reason="missing_cancel_target", deps=deps)
    if not submitter_config.config_path:
        return record_cancel_failure(context, reason="orca_config_required", deps=deps)
    return None


def cancel_stage_outcome(
    *,
    stage: dict[str, Any],
    submitter_config: SiblingSubmitterConfig,
    deps: CancellationDeps,
) -> WorkflowStageOutcome | None:
    context = cancel_stage_context(stage, deps)
    if context is None:
        return None
    skip_reason = cancel_skip_reason(context)
    if skip_reason:
        return record_cancel_skip(context, skip_reason)
    if not needs_orca_cancel(context):
        return record_local_cancel(context, deps)

    preflight_failure = remote_cancel_preflight_failure(
        context,
        submitter_config=submitter_config,
        deps=deps,
    )
    if preflight_failure is not None:
        return preflight_failure
    if not orca_submitter_matches(context.enqueue_payload, normalize_text=deps.normalize_text):
        return None
    recovery_failure = recover_intent_queue_id(
        context,
        submitter_config=submitter_config,
        deps=deps,
    )
    if recovery_failure is not None:
        return recovery_failure
    if not context.queue_id and context.submission_intent_token:
        return record_local_cancel(context, deps)
    return record_remote_cancel(
        context,
        cancel_identifier=remote_cancel_identifier(context),
        submitter_config=submitter_config,
        deps=deps,
    )


def record_workflow_cancellation_outcomes(
    *,
    payload: dict[str, Any],
    submitter_config: SiblingSubmitterConfig,
    deps: CancellationDeps,
) -> WorkflowBuckets:
    buckets = WorkflowBuckets()
    for stage in payload.get("stages", []):
        if not isinstance(stage, dict):
            continue
        outcome = cancel_stage_outcome(stage=stage, submitter_config=submitter_config, deps=deps)
        if outcome is not None:
            buckets.record(outcome)
    return buckets


def write_cancellation_summary(
    payload: dict[str, Any], buckets: WorkflowBuckets, deps: CancellationDeps
) -> None:
    if buckets.requested:
        payload["status"] = STATUS_CANCEL_REQUESTED
    elif buckets.cancelled:
        payload["status"] = STATUS_CANCELLED
    elif buckets.failed:
        payload["status"] = STATUS_CANCEL_FAILED
    metadata = workflow_metadata(payload)
    if metadata is not None:
        metadata["cancellation_summary"] = {
            "cancelled_count": len(buckets.cancelled),
            "requested_count": len(buckets.requested),
            "skipped_count": len(buckets.skipped),
            "failed_count": len(buckets.failed),
            "stage_results": buckets.stage_results,
            "updated_at": deps.now_utc_iso(),
        }


def persist_cancellation_workflow(
    *,
    workflow_root: str | Path | None,
    workspace_dir: Path,
    payload: dict[str, Any],
    deps: CancellationDeps,
) -> None:
    deps.write_workflow_payload(workspace_dir, payload)
    if workflow_root is not None:
        deps.sync_workflow_registry(workflow_root, workspace_dir, payload)


def cancellation_workflow_result(
    *,
    payload: dict[str, Any],
    workspace_dir: Path,
    buckets: WorkflowBuckets,
) -> dict[str, Any]:
    return {
        "workflow_id": payload.get("workflow_id", ""),
        "workspace_dir": str(workspace_dir),
        "status": payload.get("status", ""),
        "cancelled": buckets.cancelled,
        "requested": buckets.requested,
        "skipped": buckets.skipped,
        "failed": buckets.failed,
    }


def cancel_reaction_ts_search_workflow(
    *,
    workflow_target: str,
    workflow_root: str | Path | None,
    orca_config: str | None = None,
    orca_repo_root: str | None = None,
    deps: CancellationDeps,
) -> dict[str, Any]:
    workspace_dir = deps.resolve_workflow_workspace(
        target=workflow_target,
        workflow_root=workflow_root,
    )
    with deps.acquire_workflow_lock(workspace_dir):
        payload = deps.load_workflow_payload(workspace_dir)
        submitter_config = sibling_submitter_config(
            orca_config=orca_config,
            orca_repo_root=orca_repo_root,
            normalize_text=deps.normalize_text,
        )
        buckets = record_workflow_cancellation_outcomes(
            payload=payload,
            submitter_config=submitter_config,
            deps=deps,
        )

        write_cancellation_summary(payload, buckets, deps)
        persist_cancellation_workflow(
            workflow_root=workflow_root,
            workspace_dir=workspace_dir,
            payload=payload,
            deps=deps,
        )
        return cancellation_workflow_result(
            payload=payload,
            workspace_dir=workspace_dir,
            buckets=buckets,
        )
