from __future__ import annotations

import logging
from argparse import Namespace
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.commands.run_dir import (
    SUPPRESS_QUEUED_NOTIFICATION_CONTEXT_KEY,
    EngineRunDirSubmission,
)
from orca_auto.core.engines import entry_matches_engine_identity
from orca_auto.core.queue import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_OWNER_START_KEY,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QUEUE_RECORD_SYNC_REPAIRING,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
    DuplicateQueueEntryError,
    QueueEntry,
    QueueStatus,
)
from orca_auto.core.queue.engine.input_snapshot import (
    cleanup_unowned_input_snapshot_namespace,
)
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_QUEUE_ROOT_KEY,
    SNAPSHOT_INTENT_STATE_CREATING,
    SNAPSHOT_INTENT_STATE_ENQUEUEING,
    SNAPSHOT_INTENT_STATE_OWNED,
    SNAPSHOT_INTENT_TOKEN_KEY,
    discard_snapshot_intent,
    discard_snapshot_intent_if_generations_absent,
    transition_snapshot_intent,
)
from orca_auto.core.queue.enqueue_publication import (
    EnqueuePublicationSpec,
    repair_enqueue_publication_outcome,
    run_enqueue_publication,
)
from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.queue.store import mutate_entries, reject_active_task_duplicate
from orca_auto.core.statuses import (
    STATUS_ADMISSION_LIMIT_REACHED,
    STATUS_BLOCKED,
    STATUS_CANCEL_REQUESTED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_SUBMITTED,
    STATUS_WAITING_FOR_SLOT,
    SUBMISSION_DEFERRED_STATUSES,
)
from orca_auto.core.utils import normalize_text

from .internal_engine_models import (
    InternalEngineCommandResult,
    InternalEngineSubmitterDeps,
    InternalEngineSubmitterSpec,
    _key_value_stdout,
    _stderr_with_exception,
    _text_fields,
    internal_call_argv,
)

logger = logging.getLogger(__name__)

_QUEUED_RECORD_SYNC_KEY = QUEUE_RECORD_SYNC_KEY
_QUEUED_RECORD_SYNC_PENDING = QUEUE_RECORD_SYNC_REPAIR_PENDING


@dataclass(frozen=True)
class _InternalEngineSubmissionRequest:
    command_trace: list[str]
    args: Namespace
    job_dir: str
    priority: int


@dataclass
class _InternalEngineSubmissionState:
    resolved_job_dir: Any
    submission: Any | None = None
    entry: Any | None = None
    warning: str = ""
    deferred_reason: str = ""


class _ActiveJobDirReplay(RuntimeError):
    def __init__(self, existing: QueueEntry) -> None:
        self.existing = existing
        super().__init__(existing.queue_id)


class _ActiveJobDirCancellationPending(RuntimeError):
    def __init__(self, existing: QueueEntry) -> None:
        self.existing = existing
        super().__init__(existing.queue_id)


def _cleanup_unowned_submission_snapshot(submission: Any | None) -> None:
    if not isinstance(submission, EngineRunDirSubmission):
        return
    execution_snapshot = submission.metadata.get("execution_snapshot")
    if not isinstance(execution_snapshot, dict):
        return
    snapshot_namespace = normalize_text(execution_snapshot.get("snapshot_namespace"))
    if not snapshot_namespace:
        return
    job_dir = submission.context.get("job_dir") or submission.metadata.get("job_dir")
    if not isinstance(job_dir, (str, Path)):
        return
    cleanup_unowned_input_snapshot_namespace(
        job_dir,
        snapshot_namespace,
    )
    intent_token = normalize_text(execution_snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY))
    intent_root = normalize_text(execution_snapshot.get(SNAPSHOT_INTENT_QUEUE_ROOT_KEY))
    if intent_token and intent_root:
        discard_snapshot_intent_if_generations_absent(intent_root, intent_token)


def _submission_snapshot_intent(submission: Any) -> tuple[Path, str] | None:
    execution_snapshot = submission.metadata.get("execution_snapshot")
    if not isinstance(execution_snapshot, dict):
        return None
    intent_token = normalize_text(execution_snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY))
    intent_root = normalize_text(execution_snapshot.get(SNAPSHOT_INTENT_QUEUE_ROOT_KEY))
    if not intent_token or not intent_root:
        return None
    if (
        Path(intent_root).expanduser().resolve()
        != Path(submission.queue_root).expanduser().resolve()
    ):
        raise ValueError("Internal engine snapshot intent names another queue root")
    return Path(intent_root), intent_token


def _mark_submission_snapshot_owned(submission: Any, state: _InternalEngineSubmissionState) -> None:
    try:
        intent = _submission_snapshot_intent(submission)
        if intent is None:
            return
        intent_root, intent_token = intent
        transition_snapshot_intent(
            intent_root,
            intent_token,
            target_state=SNAPSHOT_INTENT_STATE_OWNED,
            expected_states={SNAPSHOT_INTENT_STATE_ENQUEUEING},
        )
        discard_snapshot_intent(intent_root, intent_token)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "queued snapshot ownership marker update failed; durable queue entry retains ownership: %s",
            exc,
            exc_info=True,
        )
        _append_warning(state, "queued snapshot ownership marker repair is pending")


def _active_job_dir_key(entry: QueueEntry) -> tuple[str, str] | None:
    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    job_dir = normalize_text(metadata.get("job_dir"))
    if not job_dir:
        return None
    return normalize_text(entry.app_name), str(Path(job_dir).expanduser().resolve())


def _reject_active_job_dir_duplicate(
    entries: Sequence[QueueEntry],
    entry: QueueEntry,
) -> None:
    """Keep one active execution per logical internal-engine work directory."""
    key = _active_job_dir_key(entry)
    if key is not None:
        for existing in entries:
            if existing.status not in {QueueStatus.PENDING, QueueStatus.RUNNING}:
                continue
            if _active_job_dir_key(existing) != key:
                continue
            if existing.cancel_requested:
                raise _ActiveJobDirCancellationPending(existing)
            raise _ActiveJobDirReplay(existing)
    reject_active_task_duplicate(entries, entry)


def _matches_submission_engine_identity(entry: QueueEntry, submission: Any) -> bool:
    expected_engine = normalize_text(getattr(submission, "engine", ""))
    if not entry_matches_engine_identity(entry, expected_engine):
        return False
    if any(
        normalize_text(getattr(entry, field, "")) != normalize_text(getattr(submission, field, ""))
        for field in ("app_name", "engine", "task_kind")
    ):
        return False
    if expected_engine != "xtb":
        return True
    entry_metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    submission_metadata = getattr(submission, "metadata", {})
    if not isinstance(submission_metadata, dict):
        return False
    return normalize_text(entry_metadata.get("job_type")) == normalize_text(
        submission_metadata.get("job_type")
    )


def transient_submission_block_reason(
    *, parsed_stdout: dict[str, str], stdout: str, stderr: str
) -> str:
    parsed_status = normalize_text(parsed_stdout.get("status")).lower()
    if parsed_status in SUBMISSION_DEFERRED_STATUSES:
        return parsed_status

    combined = f"{stdout}\n{stderr}".lower()
    if parsed_status == STATUS_BLOCKED and any(
        token in combined for token in ("admission", "slot", "limit")
    ):
        return STATUS_WAITING_FOR_SLOT

    patterns = (
        ("admission limit reached", STATUS_ADMISSION_LIMIT_REACHED),
        ("admission slots are full", STATUS_ADMISSION_LIMIT_REACHED),
        (STATUS_WAITING_FOR_SLOT, STATUS_WAITING_FOR_SLOT),
        ("waiting for slot", STATUS_WAITING_FOR_SLOT),
        ("no admission slot", STATUS_WAITING_FOR_SLOT),
        ("active simulation limit", STATUS_ADMISSION_LIMIT_REACHED),
        ("max_active_simulations", STATUS_ADMISSION_LIMIT_REACHED),
    )
    for pattern, reason in patterns:
        if pattern in combined:
            return reason
    return ""


def queue_submission_status(
    *,
    returncode: int,
    parsed_stdout: dict[str, str],
    stdout: str,
    stderr: str,
) -> tuple[str, str]:
    if (
        int(returncode) == 0
        and normalize_text(parsed_stdout.get("status")).lower() == STATUS_QUEUED
    ):
        return STATUS_SUBMITTED, ""
    blocked_reason = transient_submission_block_reason(
        parsed_stdout=parsed_stdout,
        stdout=stdout,
        stderr=stderr,
    )
    if blocked_reason:
        return STATUS_BLOCKED, blocked_reason
    return STATUS_FAILED, ""


def _submission_failure_payload(
    *,
    command_trace: list[str],
    job_dir: str,
    stderr: str,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parsed: dict[str, str] = {}
    status, reason = queue_submission_status(
        returncode=1,
        parsed_stdout=parsed,
        stdout="",
        stderr=stderr,
    )
    return InternalEngineCommandResult(
        status=status,
        reason=reason,
        returncode=1,
        command_argv=command_trace,
        stderr=stderr,
        parsed_stdout=parsed,
        job_dir=job_dir,
        extra_fields=dict(extra_fields or {}),
    ).to_payload()


def _submission_success_payload(
    *,
    command_trace: list[str],
    parsed: dict[str, str],
    job_dir: str,
    warning: str = "",
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return InternalEngineCommandResult(
        status=STATUS_SUBMITTED,
        returncode=0,
        command_argv=command_trace,
        stdout=_key_value_stdout(parsed),
        stderr=f"{warning}\n" if warning else "",
        parsed_stdout=parsed,
        job_id=parsed.get("job_id", ""),
        queue_id=parsed.get("queue_id", ""),
        job_dir=parsed.get("job_dir", job_dir),
        extra_fields=dict(extra_fields or {}),
    ).to_payload()


def _submission_deferred_payload(
    *,
    command_trace: list[str],
    parsed: dict[str, str],
    job_dir: str,
    reason: str,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    deferred = dict(parsed)
    deferred["status"] = STATUS_BLOCKED
    deferred["reason"] = reason
    message = "active job directory cancellation is still pending"
    return InternalEngineCommandResult(
        status=STATUS_BLOCKED,
        reason=reason,
        returncode=1,
        command_argv=command_trace,
        stdout=_key_value_stdout(deferred),
        stderr=f"{message}\n",
        parsed_stdout=deferred,
        job_id=deferred.get("job_id", ""),
        queue_id=deferred.get("queue_id", ""),
        job_dir=deferred.get("job_dir", job_dir),
        extra_fields=dict(extra_fields or {}),
    ).to_payload()


def _internal_engine_submission_request(
    *,
    api_name: str,
    job_dir: str,
    priority: int,
    config_path: str,
) -> _InternalEngineSubmissionRequest:
    priority_value = normalize_queue_priority(priority)
    return _InternalEngineSubmissionRequest(
        command_trace=internal_call_argv(
            api_name=api_name,
            config_path=config_path,
            kwargs={"job_dir": job_dir, "priority": priority_value},
        ),
        args=Namespace(config=config_path, path=job_dir, priority=priority_value),
        job_dir=job_dir,
        priority=priority_value,
    )


def _submission_extras(
    state: _InternalEngineSubmissionState,
    extra_fields_fn: Callable[[Any | None, Any | None], dict[str, Any]] | None,
) -> dict[str, Any]:
    return extra_fields_fn(state.submission, state.entry) if extra_fields_fn is not None else {}


def _resolved_job_dir_text(
    state: _InternalEngineSubmissionState,
    fallback_job_dir: str,
) -> str:
    return normalize_text(state.resolved_job_dir) or fallback_job_dir


def _submission_failure_for_state(
    *,
    request: _InternalEngineSubmissionRequest,
    state: _InternalEngineSubmissionState,
    stderr: str,
    extra_fields_fn: Callable[[Any | None, Any | None], dict[str, Any]] | None,
) -> dict[str, Any]:
    return _submission_failure_payload(
        command_trace=request.command_trace,
        job_dir=_resolved_job_dir_text(state, request.job_dir),
        stderr=stderr,
        extra_fields=_submission_extras(state, extra_fields_fn),
    )


def _append_warning(state: _InternalEngineSubmissionState, warning: str) -> None:
    state.warning = f"{state.warning}; {warning}" if state.warning else warning


def _submission_for_existing_entry(
    submission: Any,
    entry: QueueEntry,
) -> EngineRunDirSubmission:
    metadata = dict(entry.metadata)
    job_dir = Path(str(metadata.get("job_dir", ""))).expanduser().resolve()
    resource_request = metadata.get("resource_request")
    if not isinstance(resource_request, dict):
        resource_request = {}
    # Replaying an existing entry never re-announces it: the queued
    # notification was already delivered when the row was first published.
    context = {
        "job_dir": job_dir,
        "resource_request": resource_request,
        SUPPRESS_QUEUED_NOTIFICATION_CONTEXT_KEY: True,
    }
    return EngineRunDirSubmission(
        queue_root=Path(submission.queue_root).expanduser().resolve(),
        app_name=entry.app_name,
        task_id=entry.task_id,
        task_kind=entry.task_kind,
        engine=entry.engine,
        priority=entry.priority,
        metadata=metadata,
        context=context,
    )


def _same_queue_generation(current: QueueEntry, expected: QueueEntry) -> bool:
    current_job_dir = normalize_text(current.metadata.get("job_dir"))
    expected_job_dir = normalize_text(expected.metadata.get("job_dir"))
    try:
        same_job_dir = bool(current_job_dir and expected_job_dir) and (
            Path(current_job_dir).expanduser().resolve()
            == Path(expected_job_dir).expanduser().resolve()
        )
    except (OSError, RuntimeError):
        return False
    return bool(
        same_job_dir
        and current.queue_id == expected.queue_id
        and current.app_name == expected.app_name
        and current.task_id == expected.task_id
        and current.task_kind == expected.task_kind
        and current.engine == expected.engine
        and current.priority == expected.priority
        and _immutable_publication_metadata(current.metadata)
        == _immutable_publication_metadata(expected.metadata)
    )


def _immutable_publication_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    lease_keys = {
        QUEUE_RECORD_SYNC_KEY,
        QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
        QUEUE_RECORD_SYNC_OWNER_PID_KEY,
        QUEUE_RECORD_SYNC_OWNER_START_KEY,
        QUEUE_RECORD_SYNC_TOKEN_KEY,
    }
    return {key: value for key, value in metadata.items() if key not in lease_keys}


def _set_state_entry(state: _InternalEngineSubmissionState, entry: QueueEntry) -> None:
    state.entry = entry
    submission = state.submission
    if submission is not None and isinstance(submission.metadata, dict):
        submission.metadata.update(entry.metadata)


def _notification_failure_warning(state: _InternalEngineSubmissionState) -> None:
    _append_warning(
        state,
        "queued notification delivery failed; state/index recorded and notification "
        "was not retried (at-most-once delivery)",
    )


def _repair_queued_record(
    *,
    cfg: Any,
    state: _InternalEngineSubmissionState,
    record_queued_fn: Callable[[Any, Any, Any], Any],
) -> None:
    submission = state.submission
    entry = state.entry
    if submission is None or not isinstance(entry, QueueEntry):
        return

    def publish(current: QueueEntry) -> None:
        # From the moment the repair claim commits, the publication belongs to
        # the repair lease, including reconstruction of the replay submission.
        _set_state_entry(state, current)
        state.submission = _submission_for_existing_entry(submission, current)
        notification_delivered = record_queued_fn(cfg, state.submission, state.entry)
        if notification_delivered is False:
            _notification_failure_warning(state)

    outcome = repair_enqueue_publication_outcome(
        Path(submission.queue_root),
        entry,
        publish=publish,
        label=normalize_text(getattr(submission, "engine", "")) or "internal-engine",
        same_generation=_same_queue_generation,
    )
    if outcome.reason == "published":
        _append_warning(state, "queued job state/index repaired")
        return
    if outcome.entry is not None and outcome.reason not in {"failed", "claim_failed"}:
        _set_state_entry(state, outcome.entry)
        state.submission = _submission_for_existing_entry(submission, outcome.entry)
    if outcome.reason == "cancelled":
        state.deferred_reason = STATUS_CANCEL_REQUESTED
    elif outcome.reason == "invalid_state":
        raise RuntimeError(
            "queued job state/index repair refused an invalid publication state "
            f"for queue_id={outcome.entry.queue_id if outcome.entry is not None else ''}"
        )
    elif outcome.reason == "running":
        _append_warning(
            state,
            "queued job state/index repair skipped because the worker is running",
        )
    elif outcome.reason == "identity_changed":
        _append_warning(
            state,
            "queued job state/index repair refused a changed queue generation",
        )
    elif outcome.reason in {"failed", "claim_failed", "missing"}:
        error = outcome.error
        detail = (
            f" ({error.__class__.__name__}: {error})"
            if error is not None
            else " (queue entry disappeared)"
        )
        stage = "repair claim" if outcome.reason in {"claim_failed", "missing"} else "repair"
        logger.warning(
            "queued job record %s failed after queue submission succeeded: job_dir=%s queue_id=%s",
            stage,
            state.resolved_job_dir,
            entry.queue_id,
        )
        _append_warning(
            state,
            f"queued job record {stage} failed; queue submission succeeded{detail}",
        )


def repair_internal_engine_queue_publication(
    *,
    cfg: Any,
    queue_root: Path,
    entry: QueueEntry,
    record_queued_fn: Callable[[Any, Any, Any], Any],
    entry_matches_fn: Callable[[QueueEntry], bool],
) -> bool:
    """Repair one internal-engine queued record before a worker may claim it."""

    if not entry_matches_fn(entry):
        return True
    if entry.status != QueueStatus.PENDING or entry.cancel_requested:
        return True
    sync_state = normalize_text(entry.metadata.get(QUEUE_RECORD_SYNC_KEY)).lower()
    if not sync_state or sync_state == QUEUE_RECORD_SYNC_COMPLETE:
        return True
    if sync_state not in {
        QUEUE_RECORD_SYNC_PREPARING,
        QUEUE_RECORD_SYNC_REPAIR_PENDING,
        QUEUE_RECORD_SYNC_REPAIRING,
    }:
        logger.error(
            "Cannot repair internal-engine queue publication with invalid state %r: queue_id=%s",
            sync_state,
            entry.queue_id,
        )
        return False

    raw_job_dir = normalize_text(entry.metadata.get("job_dir"))
    if not raw_job_dir:
        logger.error(
            "Cannot repair internal-engine queue publication without job_dir: queue_id=%s",
            entry.queue_id,
        )
        return False
    try:
        resolved_job_dir = Path(raw_job_dir).expanduser().resolve()
        resolved_queue_root = queue_root.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    if not resolved_job_dir.is_relative_to(resolved_queue_root):
        logger.error(
            "Cannot repair internal-engine publication outside its queue root: "
            "queue_id=%s job_dir=%s queue_root=%s",
            entry.queue_id,
            resolved_job_dir,
            resolved_queue_root,
        )
        return False
    selected_input_text = normalize_text(entry.metadata.get("selected_input_xyz"))
    if selected_input_text:
        try:
            selected_input = Path(selected_input_text).expanduser().resolve()
        except (OSError, RuntimeError):
            return False
        if not selected_input.is_relative_to(resolved_job_dir):
            return False
    resource_request = entry.metadata.get("resource_request")
    if not isinstance(resource_request, dict):
        resource_request = {}
    base_submission = EngineRunDirSubmission(
        queue_root=resolved_queue_root,
        app_name=entry.app_name,
        task_id=entry.task_id,
        task_kind=entry.task_kind,
        engine=entry.engine,
        priority=entry.priority,
        metadata=dict(entry.metadata),
        context={
            "job_dir": resolved_job_dir,
            "resource_request": resource_request,
            SUPPRESS_QUEUED_NOTIFICATION_CONTEXT_KEY: True,
        },
    )
    state = _InternalEngineSubmissionState(
        resolved_job_dir=resolved_job_dir,
        submission=base_submission,
        entry=entry,
    )
    _repair_queued_record(
        cfg=cfg,
        state=state,
        record_queued_fn=record_queued_fn,
    )

    def reload(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        current = next((item for item in entries if item.queue_id == entry.queue_id), None)
        return current, False

    current = mutate_entries(resolved_queue_root, reload)
    if current is None or current.status != QueueStatus.PENDING or current.cancel_requested:
        return True
    return (
        normalize_text(current.metadata.get(QUEUE_RECORD_SYNC_KEY)).lower()
        == QUEUE_RECORD_SYNC_COMPLETE
    )


def _enqueue_internal_engine_submission(
    *,
    request: _InternalEngineSubmissionRequest,
    state: _InternalEngineSubmissionState,
    load_config_fn: Callable[[Any], Any],
    resolve_job_dir_fn: Callable[[Any, str], Any],
    load_manifest_fn: Callable[[Any], dict[str, Any]],
    build_submission_fn: Callable[[Any, Any, dict[str, Any], Any], Any],
    record_queued_fn: Callable[[Any, Any, Any], Any],
    enqueue_fn: Callable[..., Any],
) -> None:
    cfg = load_config_fn(request.args.config)
    state.resolved_job_dir = resolve_job_dir_fn(cfg, request.job_dir)
    manifest = load_manifest_fn(state.resolved_job_dir)
    state.submission = build_submission_fn(cfg, state.resolved_job_dir, manifest, request.args)
    submission = state.submission
    try:
        intent = _submission_snapshot_intent(submission)
        if intent is not None:
            intent_root, intent_token = intent
            transition_snapshot_intent(
                intent_root,
                intent_token,
                target_state=SNAPSHOT_INTENT_STATE_ENQUEUEING,
                expected_states={SNAPSHOT_INTENT_STATE_CREATING},
            )
    except BaseException:
        _cleanup_unowned_submission_snapshot(submission)
        raise

    def publish(current: QueueEntry) -> None:
        _set_state_entry(state, current)
        notification_delivered = record_queued_fn(cfg, state.submission, state.entry)
        if notification_delivered is False:
            _notification_failure_warning(state)

    spec = EnqueuePublicationSpec(
        queue_root=Path(submission.queue_root),
        app_name=submission.app_name,
        task_id=submission.task_id,
        task_kind=submission.task_kind,
        engine=submission.engine,
        priority=submission.priority,
        metadata=dict(submission.metadata),
        label=normalize_text(getattr(submission, "engine", "")) or "internal-engine",
        publish=publish,
        duplicate_policy=_reject_active_job_dir_duplicate,
        enqueue_fn=enqueue_fn,
        on_compensated_failure=lambda: _cleanup_unowned_submission_snapshot(submission),
        same_generation=_same_queue_generation,
    )
    try:
        outcome = run_enqueue_publication(spec)
    except _ActiveJobDirCancellationPending as pending:
        # The driver already ran the compensated-failure cleanup hook before
        # re-raising, so the unowned snapshot is gone.
        state.entry = pending.existing
        state.submission = _submission_for_existing_entry(submission, pending.existing)
        state.deferred_reason = STATUS_CANCEL_REQUESTED
        return
    except _ActiveJobDirReplay as replay:
        if not _matches_submission_engine_identity(replay.existing, submission):
            raise RuntimeError(
                "active job directory is owned by a conflicting queue identity "
                f"(queue_id={replay.existing.queue_id})"
            ) from replay
        state.entry = replay.existing
        state.submission = _submission_for_existing_entry(submission, replay.existing)
        _append_warning(
            state,
            "active job directory submission replayed; reused existing queue entry "
            f"(queue_id={replay.existing.queue_id}, task_id={replay.existing.task_id})",
        )
        _repair_queued_record(
            cfg=cfg,
            state=state,
            record_queued_fn=record_queued_fn,
        )
        return
    _set_state_entry(state, outcome.entry)
    _mark_submission_snapshot_owned(submission, state)
    if outcome.cancelled:
        state.deferred_reason = STATUS_CANCEL_REQUESTED
        return
    for warning in outcome.warnings:
        _append_warning(state, warning)


def _queued_submission_fields(
    *,
    state: _InternalEngineSubmissionState,
    extra_fields: dict[str, Any],
) -> dict[str, str]:
    submission = state.submission
    entry = state.entry
    if submission is None or entry is None:
        raise RuntimeError("internal engine submission did not produce a queue entry")
    return _text_fields(
        {
            "status": STATUS_QUEUED,
            "job_dir": state.resolved_job_dir,
            "job_id": getattr(entry, "task_id", "") or submission.task_id,
            "queue_id": getattr(entry, "queue_id", ""),
            "priority": getattr(entry, "priority", submission.priority),
            **extra_fields,
        }
    )


def submit_internal_engine_job_dir(
    *,
    load_config_fn: Callable[[Any], Any],
    resolve_job_dir_fn: Callable[[Any, str], Any],
    load_manifest_fn: Callable[[Any], dict[str, Any]],
    build_submission_fn: Callable[[Any, Any, dict[str, Any], Any], Any],
    record_queued_fn: Callable[[Any, Any, Any], Any],
    enqueue_fn: Callable[..., Any],
    api_name: str,
    job_dir: str,
    priority: int,
    config_path: str,
    extra_fields_fn: Callable[[Any | None, Any | None], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    request = _internal_engine_submission_request(
        api_name=api_name,
        job_dir=job_dir,
        priority=priority,
        config_path=config_path,
    )
    state = _InternalEngineSubmissionState(resolved_job_dir=job_dir)
    try:
        _enqueue_internal_engine_submission(
            request=request,
            state=state,
            load_config_fn=load_config_fn,
            resolve_job_dir_fn=resolve_job_dir_fn,
            load_manifest_fn=load_manifest_fn,
            build_submission_fn=build_submission_fn,
            record_queued_fn=record_queued_fn,
            enqueue_fn=enqueue_fn,
        )
    except DuplicateQueueEntryError as exc:
        return _submission_failure_for_state(
            request=request,
            state=state,
            stderr=_stderr_with_exception("", exc),
            extra_fields_fn=extra_fields_fn,
        )
    except Exception as exc:  # noqa: BLE001
        return _submission_failure_for_state(
            request=request,
            state=state,
            stderr=_stderr_with_exception("", exc),
            extra_fields_fn=extra_fields_fn,
        )

    extras = _submission_extras(state, extra_fields_fn)
    parsed = _queued_submission_fields(state=state, extra_fields=extras)
    if state.deferred_reason:
        return _submission_deferred_payload(
            command_trace=request.command_trace,
            parsed=parsed,
            job_dir=_resolved_job_dir_text(state, request.job_dir),
            reason=state.deferred_reason,
            extra_fields=extras,
        )
    if state.warning:
        parsed["warning"] = state.warning
    return _submission_success_payload(
        command_trace=request.command_trace,
        parsed=parsed,
        job_dir=_resolved_job_dir_text(state, request.job_dir),
        warning=state.warning,
        extra_fields=extras,
    )


def submit_engine_job_dir(
    *,
    spec: InternalEngineSubmitterSpec,
    deps: InternalEngineSubmitterDeps,
    job_dir: str,
    priority: int,
    config_path: str,
) -> dict[str, Any]:
    return submit_internal_engine_job_dir(
        load_config_fn=deps.load_config_fn,
        resolve_job_dir_fn=deps.resolve_job_dir_fn,
        load_manifest_fn=deps.load_manifest_fn,
        build_submission_fn=deps.build_submission_fn,
        record_queued_fn=deps.record_queued_fn,
        enqueue_fn=deps.enqueue_fn,
        api_name=spec.run_dir_api_name,
        config_path=config_path,
        job_dir=job_dir,
        priority=priority,
        extra_fields_fn=spec.extra_fields_fn,
    )
