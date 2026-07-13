from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.app_ids import ORCA_SUBMITTERS
from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.queue.publication import QUEUE_SUBMISSION_INTENT_KEY
from orca_auto.core.statuses import (
    STATUS_ADMISSION_BLOCKED,
    STATUS_ADMISSION_LIMIT_REACHED,
    STATUS_BLOCKED,
    STATUS_DEFERRED,
    STATUS_FAILED,
    STATUS_PARTIALLY_SUBMITTED,
    STATUS_PLANNED,
    STATUS_QUEUED,
    STATUS_SKIPPED,
    STATUS_SUBMISSION_FAILED,
    STATUS_SUBMITTED,
    STATUS_WAITING_FOR_SLOT,
    SUBMISSION_DEFERRED_STATUSES,
    SUBMISSION_SUBMITTED_STAGE_STATUSES,
    SUBMISSION_SUBMITTED_TASK_STATUSES,
)
from orca_auto.core.utils import timestamped_token
from orca_auto.flow.orchestration.stage_views import (
    WorkflowPayloadView,
    WorkflowStageView,
    WorkflowTaskView,
)

from .orca_models import (
    RecordedStageTransition,
    SiblingSubmitterConfig,
    TaskRecordMutator,
    WorkflowBuckets,
    WorkflowStageOutcome,
    ensure_submission_metadata,
    mapping_payload,
    sibling_submitter_config,
)


@dataclass(frozen=True)
class _SubmissionStatusNames:
    admission_blocked: str = STATUS_ADMISSION_BLOCKED
    admission_limit_reached: str = STATUS_ADMISSION_LIMIT_REACHED
    blocked: str = STATUS_BLOCKED
    deferred: str = STATUS_DEFERRED
    failed: str = STATUS_FAILED
    partially_submitted: str = STATUS_PARTIALLY_SUBMITTED
    planned: str = STATUS_PLANNED
    queued: str = STATUS_QUEUED
    skipped: str = STATUS_SKIPPED
    submitted: str = STATUS_SUBMITTED
    submission_failed: str = STATUS_SUBMISSION_FAILED
    waiting_for_slot: str = STATUS_WAITING_FOR_SLOT


@dataclass(frozen=True)
class _SubmissionBucketNames:
    deferred: str = STATUS_DEFERRED
    failed: str = STATUS_FAILED
    skipped: str = STATUS_SKIPPED
    submitted: str = STATUS_SUBMITTED


_STATUS = _SubmissionStatusNames()
_BUCKET = _SubmissionBucketNames()
_DEFERRED_SUBMISSION_STATUSES = SUBMISSION_DEFERRED_STATUSES
_SUBMITTED_TASK_STATUSES = SUBMISSION_SUBMITTED_TASK_STATUSES
_SUBMITTED_STAGE_STATUSES = SUBMISSION_SUBMITTED_STAGE_STATUSES


@dataclass(frozen=True)
class SubmissionDeps:
    normalize_text: Callable[[Any], str]
    now_utc_iso: Callable[[], str]
    resolve_workflow_workspace: Callable[..., Path]
    acquire_workflow_lock: Callable[[Path], AbstractContextManager[None]]
    load_workflow_payload: Callable[[Path], dict[str, Any]]
    write_workflow_payload: Callable[[Path, dict[str, Any]], Any]
    sync_workflow_registry: Callable[[str | Path, Path, dict[str, Any]], Any]
    submit_reaction_dir: Callable[..., dict[str, Any]]
    recover_exact_reaction_dir_submission: Callable[..., dict[str, Any] | None]


@dataclass(frozen=True)
class _SubmissionStageContext:
    stage: WorkflowStageView
    task: WorkflowTaskView
    enqueue_payload: dict[str, Any]
    stage_metadata: dict[str, Any]

    @classmethod
    def from_stage(
        cls,
        stage: dict[str, Any],
    ) -> _SubmissionStageContext | None:
        stage_view = WorkflowStageView.from_raw(stage)
        if stage_view is None:
            return None
        task_view = stage_view.existing_task
        if task_view is None:
            return None
        enqueue_payload = task_view.existing_enqueue_payload()
        if enqueue_payload is None:
            return None
        return cls(
            stage=stage_view,
            task=task_view,
            enqueue_payload=enqueue_payload,
            stage_metadata=ensure_submission_metadata(stage_view.raw, task_view.raw),
        )

    @property
    def stage_id(self) -> Any:
        return self.stage.raw.get("stage_id", "")

    def reaction_dir(self, normalize_text: Callable[[Any], str]) -> str:
        return normalize_text(self.enqueue_payload.get("reaction_dir"))

    def should_submit_to_orca(self, normalize_text: Callable[[Any], str]) -> bool:
        return orca_submitter_matches(self.enqueue_payload, normalize_text=normalize_text)


@dataclass(frozen=True)
class _SubmissionStageRequest:
    reaction_dir: str
    priority: int
    config_path: str
    repo_root: str | None
    submitter_kwargs: dict[str, Any]

    def call_kwargs(self) -> dict[str, Any]:
        return {
            "reaction_dir": self.reaction_dir,
            "priority": self.priority,
            "config_path": self.config_path,
            "repo_root": self.repo_root,
            **self.submitter_kwargs,
        }


SUBMISSION_RESULT = TaskRecordMutator("submission_result")


def submission_is_deferred(value: dict[str, Any], *, normalize_text: Callable[[Any], str]) -> bool:
    return normalize_text(value.get("status")).lower() in _DEFERRED_SUBMISSION_STATUSES


def submission_deferred_reason(
    value: dict[str, Any],
    *,
    normalize_text: Callable[[Any], str],
) -> str:
    return (
        normalize_text(value.get("reason"))
        or normalize_text(value.get("status"))
        or _STATUS.waiting_for_slot
    )


def skip_submission_reason(
    *,
    stage: dict[str, Any],
    task: dict[str, Any],
    skip_submitted: bool,
    normalize_text: Callable[[Any], str],
) -> str:
    if not skip_submitted:
        return ""
    stage_view = WorkflowStageView(stage)
    task_view = WorkflowTaskView(task)
    stage_status = stage_view.status_with(normalize_text)
    task_status = task_view.status_with(normalize_text)
    if (
        task_view.has_submitted_result()
        or task_status in _SUBMITTED_TASK_STATUSES
        or stage_status in _SUBMITTED_STAGE_STATUSES
    ):
        return "already_submitted"
    return ""


def submission_resource_kwargs(enqueue_payload: dict[str, Any]) -> dict[str, int]:
    resource_kwargs: dict[str, int] = {}
    max_cores = int(enqueue_payload.get("max_cores", 0) or 0)
    max_memory_gb = int(enqueue_payload.get("max_memory_gb", 0) or 0)
    if max_cores > 0:
        resource_kwargs["max_cores"] = max_cores
    if max_memory_gb > 0:
        resource_kwargs["max_memory_gb"] = max_memory_gb
    return resource_kwargs


def submission_force(enqueue_payload: dict[str, Any]) -> bool:
    value = enqueue_payload.get("force", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def record_missing_reaction_dir(
    *,
    stage: dict[str, Any],
    task: dict[str, Any],
    stage_metadata: dict[str, Any],
    now_utc_iso: Callable[[], str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    submission_record = {
        "status": _STATUS.failed,
        "reason": "missing_reaction_dir",
        "submitted_at": now_utc_iso(),
    }
    stage_id = stage.get("stage_id", "")
    SUBMISSION_RESULT.apply(
        stage=stage,
        task=task,
        stage_metadata=stage_metadata,
        task_record=submission_record,
        task_status=_STATUS.submission_failed,
        stage_status=_STATUS.submission_failed,
        metadata_updates={
            "submission_status": _STATUS.submission_failed,
            "submitted_at": submission_record["submitted_at"],
        },
    )
    return (
        {"stage_id": stage_id, "reason": "missing_reaction_dir"},
        {
            "stage_id": stage_id,
            "status": _STATUS.submission_failed,
            "reason": "missing_reaction_dir",
        },
    )


def submitted_stage_transition(
    *,
    stage_id: str,
    stdout_payload: dict[str, Any],
    reaction_dir: str,
    submitted_at: str,
    returncode: int,
) -> RecordedStageTransition:
    queue_id = stdout_payload.get("queue_id", "")
    return RecordedStageTransition(
        bucket=_BUCKET.submitted,
        detail={
            "stage_id": stage_id,
            "queue_id": queue_id,
            "reaction_dir": stdout_payload.get("job_dir")
            or stdout_payload.get("reaction_dir", reaction_dir),
        },
        stage_result={
            "stage_id": stage_id,
            "status": _STATUS.submitted,
            "queue_id": queue_id,
            "returncode": returncode,
        },
        mutation=SUBMISSION_RESULT.mutation(
            task_status=_STATUS.submitted,
            stage_status=_STATUS.queued,
            metadata_updates={
                "queue_id": queue_id,
                "submission_status": _STATUS.submitted,
                "submitted_at": submitted_at,
            },
            metadata_removals=("submission_deferred_reason", "last_submission_attempt_at"),
        ),
    )


def deferred_submission_transition(
    *,
    stage_id: str,
    submission_record: dict[str, Any],
    submitted_at: str,
    returncode: int,
    normalize_text: Callable[[Any], str],
) -> RecordedStageTransition:
    reason = submission_deferred_reason(submission_record, normalize_text=normalize_text)
    return RecordedStageTransition(
        bucket=_BUCKET.deferred,
        detail={
            "stage_id": stage_id,
            "reason": reason,
        },
        stage_result={
            "stage_id": stage_id,
            "status": _STATUS.waiting_for_slot,
            "reason": reason,
            "returncode": returncode,
        },
        mutation=SUBMISSION_RESULT.mutation(
            task_status=_STATUS.planned,
            stage_status=_STATUS.planned,
            metadata_updates={
                "submission_status": _STATUS.waiting_for_slot,
                "submission_deferred_reason": reason,
                "last_submission_attempt_at": submitted_at,
            },
            metadata_removals=("submitted_at", "queue_id"),
        ),
    )


def failed_submission_transition(
    *,
    stage_id: str,
    stdout_payload: dict[str, Any],
    submission_record: dict[str, Any],
    submitted_at: str,
    returncode: int,
) -> RecordedStageTransition:
    return RecordedStageTransition(
        bucket=_BUCKET.failed,
        detail={
            "stage_id": stage_id,
            "returncode": returncode,
            "stderr": str(submission_record.get("stderr", "")).strip(),
            "stdout": str(submission_record.get("stdout", "")).strip(),
        },
        stage_result={
            "stage_id": stage_id,
            "status": _STATUS.submission_failed,
            "queue_id": stdout_payload.get("queue_id", ""),
            "returncode": returncode,
        },
        mutation=SUBMISSION_RESULT.mutation(
            task_status=_STATUS.submission_failed,
            stage_status=_STATUS.submission_failed,
            metadata_updates={
                "submission_status": _STATUS.submission_failed,
                "submitted_at": submitted_at,
            },
            metadata_removals=("submission_deferred_reason", "last_submission_attempt_at"),
        ),
    )


def submission_transition(
    *,
    stage_id: str,
    reaction_dir: str,
    submission_record: dict[str, Any],
    stdout_payload: dict[str, Any],
    returncode: int,
    normalize_text: Callable[[Any], str],
) -> RecordedStageTransition:
    submitted_at = submission_record["submitted_at"]
    if submission_record["status"] == _STATUS.submitted:
        return submitted_stage_transition(
            stage_id=stage_id,
            stdout_payload=stdout_payload,
            reaction_dir=reaction_dir,
            submitted_at=submitted_at,
            returncode=returncode,
        )
    if submission_is_deferred(submission_record, normalize_text=normalize_text):
        return deferred_submission_transition(
            stage_id=stage_id,
            submission_record=submission_record,
            submitted_at=submitted_at,
            returncode=returncode,
            normalize_text=normalize_text,
        )
    return failed_submission_transition(
        stage_id=stage_id,
        stdout_payload=stdout_payload,
        submission_record=submission_record,
        submitted_at=submitted_at,
        returncode=returncode,
    )


def record_submission_outcome(
    *,
    stage: dict[str, Any],
    task: dict[str, Any],
    stage_metadata: dict[str, Any],
    reaction_dir: str,
    submission_record: dict[str, Any],
    now_utc_iso: Callable[[], str],
    normalize_text: Callable[[Any], str],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    stage_id = stage.get("stage_id", "")
    stdout_payload = mapping_payload(submission_record.get("parsed_stdout"))
    returncode = int(submission_record.get("returncode", 1))
    submission_record["submitted_at"] = now_utc_iso()
    transition = submission_transition(
        stage_id=stage_id,
        reaction_dir=reaction_dir,
        submission_record=submission_record,
        stdout_payload=stdout_payload,
        returncode=returncode,
        normalize_text=normalize_text,
    )
    SUBMISSION_RESULT.apply(
        stage=stage,
        task=task,
        stage_metadata=stage_metadata,
        mutation=transition.mutation,
        task_record=submission_record,
    )
    return (
        transition.bucket,
        transition.detail,
        transition.stage_result,
    )


def submission_summary_state(
    *,
    submitted: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    failed: list[dict[str, Any]],
) -> tuple[str | None, str]:
    if failed and submitted:
        return _STATUS.queued, _STATUS.partially_submitted
    if failed:
        return _STATUS.submission_failed, _STATUS.submission_failed
    if submitted:
        return _STATUS.queued, _STATUS.submitted
    if skipped:
        return None, _STATUS.skipped
    return None, ""


def orca_submitter_matches(
    enqueue_payload: dict[str, Any],
    *,
    normalize_text: Callable[[Any], str],
) -> bool:
    return normalize_text(enqueue_payload.get("submitter")) in {"", *ORCA_SUBMITTERS}


def submission_kwargs(enqueue_payload: dict[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = submission_resource_kwargs(enqueue_payload)
    if submission_force(enqueue_payload):
        kwargs["force"] = True
    return kwargs


def submission_stage_request(
    *,
    context: _SubmissionStageContext,
    submitter_config: SiblingSubmitterConfig,
    reaction_dir: str,
) -> _SubmissionStageRequest:
    submitter_kwargs = submission_kwargs(context.enqueue_payload)
    intent_token = str(context.stage_metadata.get(QUEUE_SUBMISSION_INTENT_KEY) or "").strip()
    if intent_token:
        submitter_kwargs[QUEUE_SUBMISSION_INTENT_KEY] = intent_token
    return _SubmissionStageRequest(
        reaction_dir=reaction_dir,
        priority=normalize_queue_priority(context.enqueue_payload.get("priority")),
        config_path=submitter_config.config_path,
        repo_root=submitter_config.repo_root,
        submitter_kwargs=submitter_kwargs,
    )


def ensure_submission_intents(payload: dict[str, Any], deps: SubmissionDeps) -> bool:
    changed = False
    for stage_view in WorkflowPayloadView(payload).stage_views:
        context = _SubmissionStageContext.from_stage(stage_view.raw)
        if context is None or not context.should_submit_to_orca(deps.normalize_text):
            continue
        if not context.reaction_dir(deps.normalize_text):
            continue
        if skip_submission_reason(
            stage=context.stage.raw,
            task=context.task.raw,
            skip_submitted=True,
            normalize_text=deps.normalize_text,
        ):
            continue
        if str(context.stage_metadata.get(QUEUE_SUBMISSION_INTENT_KEY) or "").strip():
            continue
        context.stage_metadata[QUEUE_SUBMISSION_INTENT_KEY] = timestamped_token(
            "orca_workflow_submission",
            token_bytes=16,
        )
        changed = True
    return changed


def submit_stage_request(
    request: _SubmissionStageRequest,
    deps: SubmissionDeps,
) -> dict[str, Any]:
    return deps.submit_reaction_dir(**request.call_kwargs())


def recover_submission_conflict(
    request: _SubmissionStageRequest,
    submission_record: dict[str, Any],
    deps: SubmissionDeps,
) -> dict[str, Any]:
    if (
        not submission_is_deferred(
            submission_record,
            normalize_text=deps.normalize_text,
        )
        or submission_deferred_reason(
            submission_record,
            normalize_text=deps.normalize_text,
        )
        != "submission_conflict"
    ):
        return submission_record
    recovered = deps.recover_exact_reaction_dir_submission(**request.call_kwargs())
    return recovered if recovered is not None else submission_record


def skipped_submission_outcome(stage_id: Any, reason: str) -> WorkflowStageOutcome:
    return WorkflowStageOutcome(
        bucket=_BUCKET.skipped,
        detail={"stage_id": stage_id, "reason": reason},
        stage_result={"stage_id": stage_id, "status": _STATUS.skipped, "reason": reason},
    )


def missing_reaction_dir_outcome(
    context: _SubmissionStageContext,
    deps: SubmissionDeps,
) -> WorkflowStageOutcome:
    fail_record, stage_result = record_missing_reaction_dir(
        stage=context.stage.raw,
        task=context.task.raw,
        stage_metadata=context.stage_metadata,
        now_utc_iso=deps.now_utc_iso,
    )
    return WorkflowStageOutcome(
        bucket=_BUCKET.failed,
        detail=fail_record,
        stage_result=stage_result,
    )


def workflow_submission_outcome(
    *,
    context: _SubmissionStageContext,
    reaction_dir: str,
    submission_record: dict[str, Any],
    deps: SubmissionDeps,
) -> WorkflowStageOutcome:
    outcome, detail_record, stage_result = record_submission_outcome(
        stage=context.stage.raw,
        task=context.task.raw,
        stage_metadata=context.stage_metadata,
        reaction_dir=reaction_dir,
        submission_record=submission_record,
        now_utc_iso=deps.now_utc_iso,
        normalize_text=deps.normalize_text,
    )
    bucket = _BUCKET.skipped if outcome == _BUCKET.deferred else outcome
    return WorkflowStageOutcome(bucket=bucket, detail=detail_record, stage_result=stage_result)


def submission_stage_outcome(
    *,
    stage: dict[str, Any],
    submitter_config: SiblingSubmitterConfig,
    skip_submitted: bool,
    deps: SubmissionDeps,
) -> WorkflowStageOutcome | None:
    context = _SubmissionStageContext.from_stage(stage)
    if context is None:
        return None

    skip_reason = skip_submission_reason(
        stage=context.stage.raw,
        task=context.task.raw,
        skip_submitted=skip_submitted,
        normalize_text=deps.normalize_text,
    )
    stage_id = context.stage_id
    if skip_reason:
        return skipped_submission_outcome(stage_id, skip_reason)

    reaction_dir = context.reaction_dir(deps.normalize_text)
    if not reaction_dir:
        return missing_reaction_dir_outcome(context, deps)
    if not context.should_submit_to_orca(deps.normalize_text):
        return None

    request = submission_stage_request(
        context=context,
        submitter_config=submitter_config,
        reaction_dir=reaction_dir,
    )
    submission_record = deps.recover_exact_reaction_dir_submission(**request.call_kwargs())
    if submission_record is None:
        submission_record = recover_submission_conflict(
            request,
            submit_stage_request(request, deps),
            deps,
        )
    return workflow_submission_outcome(
        context=context,
        reaction_dir=reaction_dir,
        submission_record=submission_record,
        deps=deps,
    )


def record_workflow_submission_outcomes(
    *,
    payload: dict[str, Any],
    submitter_config: SiblingSubmitterConfig,
    skip_submitted: bool,
    deps: SubmissionDeps,
) -> WorkflowBuckets:
    buckets = WorkflowBuckets()
    payload_view = WorkflowPayloadView(payload)
    for stage_view in payload_view.stage_views:
        outcome = submission_stage_outcome(
            stage=stage_view.raw,
            submitter_config=submitter_config,
            skip_submitted=skip_submitted,
            deps=deps,
        )
        if outcome is not None:
            buckets.record(outcome)
    return buckets


def record_submission_summary(
    payload: dict[str, Any], buckets: WorkflowBuckets, deps: SubmissionDeps
) -> None:
    payload_status, summary_status = submission_summary_state(
        submitted=buckets.submitted,
        skipped=buckets.skipped,
        failed=buckets.failed,
    )
    payload_view = WorkflowPayloadView(payload)
    if payload_status:
        payload_view.set_status(payload_status)
    metadata = payload_view.metadata()
    if metadata is not None:
        metadata["submission_summary"] = {
            "status": summary_status,
            "submitted_count": len(buckets.submitted),
            "skipped_count": len(buckets.skipped),
            "failed_count": len(buckets.failed),
            "stage_results": buckets.stage_results,
            "updated_at": deps.now_utc_iso(),
        }


def persist_submission_workflow(
    *,
    workflow_root: str | Path | None,
    workspace_dir: Path,
    payload: dict[str, Any],
    deps: SubmissionDeps,
) -> None:
    deps.write_workflow_payload(workspace_dir, payload)
    if workflow_root is not None:
        deps.sync_workflow_registry(workflow_root, workspace_dir, payload)


def submission_workflow_result(
    *,
    payload: dict[str, Any],
    workspace_dir: Path,
    buckets: WorkflowBuckets,
) -> dict[str, Any]:
    payload_view = WorkflowPayloadView(payload)
    return {
        "workflow_id": payload_view.raw.get("workflow_id", ""),
        "workspace_dir": str(workspace_dir),
        "status": payload_view.raw.get("status", ""),
        "submitted": buckets.submitted,
        "skipped": buckets.skipped,
        "failed": buckets.failed,
    }


def submit_reaction_ts_search_workflow(
    *,
    workflow_target: str,
    workflow_root: str | Path | None,
    orca_config: str,
    orca_repo_root: str | None = None,
    skip_submitted: bool = True,
    deps: SubmissionDeps,
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
        if ensure_submission_intents(payload, deps):
            deps.write_workflow_payload(workspace_dir, payload)
        buckets = record_workflow_submission_outcomes(
            payload=payload,
            submitter_config=submitter_config,
            skip_submitted=skip_submitted,
            deps=deps,
        )

        record_submission_summary(payload, buckets, deps)
        persist_submission_workflow(
            workflow_root=workflow_root,
            workspace_dir=workspace_dir,
            payload=payload,
            deps=deps,
        )
        return submission_workflow_result(
            payload=payload,
            workspace_dir=workspace_dir,
            buckets=buckets,
        )
