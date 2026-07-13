from __future__ import annotations

import logging
import os
from argparse import Namespace
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.commands.run_dir import (
    SUPPRESS_QUEUED_NOTIFICATION_CONTEXT_KEY,
    EngineRunDirSubmission,
)
from orca_auto.core.queue import (
    QUEUE_RECORD_SYNC_ABORTED,
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
    process_start_token,
    queue_record_publication_lock,
)
from orca_auto.core.queue.engine.input_snapshot import cleanup_unowned_input_snapshot_namespace
from orca_auto.core.queue.internal_engine.runtime import entry_matches_engine_identity
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
from orca_auto.core.utils import normalize_text, now_utc_iso, timestamped_token

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
_QUEUED_RECORD_SYNC_PREPARING = QUEUE_RECORD_SYNC_PREPARING
_QUEUED_RECORD_SYNC_REPAIRING = QUEUE_RECORD_SYNC_REPAIRING
_QUEUED_RECORD_SYNC_COMPLETE = QUEUE_RECORD_SYNC_COMPLETE


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
    publication_token: str = ""


class _ActiveJobDirReplay(RuntimeError):
    def __init__(self, existing: QueueEntry) -> None:
        self.existing = existing
        super().__init__(existing.queue_id)


class _ActiveJobDirCancellationPending(RuntimeError):
    def __init__(self, existing: QueueEntry) -> None:
        self.existing = existing
        super().__init__(existing.queue_id)


class _EnqueueOwnershipUnknown(RuntimeError):
    """Raised when a failed enqueue cannot be proven committed or uncommitted."""


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
    *,
    suppress_queued_notification: bool = True,
) -> EngineRunDirSubmission:
    metadata = dict(entry.metadata)
    job_dir = Path(str(metadata.get("job_dir", ""))).expanduser().resolve()
    resource_request = metadata.get("resource_request")
    if not isinstance(resource_request, dict):
        resource_request = {}
    context = {
        "job_dir": job_dir,
        "resource_request": resource_request,
    }
    if suppress_queued_notification:
        context[SUPPRESS_QUEUED_NOTIFICATION_CONTEXT_KEY] = True
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


def _queued_record_sync_metadata(
    sync_state: str,
    *,
    token: str,
    owner_pid: int,
) -> dict[str, Any]:
    return {
        QUEUE_RECORD_SYNC_KEY: sync_state,
        QUEUE_RECORD_SYNC_UPDATED_AT_KEY: now_utc_iso(),
        QUEUE_RECORD_SYNC_OWNER_PID_KEY: owner_pid,
        QUEUE_RECORD_SYNC_OWNER_START_KEY: (
            process_start_token(owner_pid) if owner_pid > 0 else ""
        ),
        QUEUE_RECORD_SYNC_TOKEN_KEY: token,
    }


def _set_state_entry(state: _InternalEngineSubmissionState, entry: QueueEntry) -> None:
    state.entry = entry
    submission = state.submission
    if submission is not None and isinstance(submission.metadata, dict):
        submission.metadata.update(entry.metadata)


def _transition_owned_queued_record_sync(
    state: _InternalEngineSubmissionState,
    *,
    expected_token: str,
    expected_state: str,
    target_state: str,
    owner_pid: int,
) -> bool:
    submission = state.submission
    entry = state.entry
    if submission is None or not isinstance(entry, QueueEntry):
        return True

    def transition(entries: list[QueueEntry]) -> tuple[tuple[QueueEntry, bool], bool]:
        for index, current in enumerate(entries):
            if current.queue_id != entry.queue_id:
                continue
            if not _same_queue_generation(current, entry):
                return (current, False), False
            current_sync_state = normalize_text(current.metadata.get(QUEUE_RECORD_SYNC_KEY)).lower()
            current_token = normalize_text(current.metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY))
            if current_sync_state == QUEUE_RECORD_SYNC_COMPLETE:
                return (current, True), False
            if (
                current.status != QueueStatus.PENDING
                or current_sync_state != expected_state
                or current_token != expected_token
            ):
                return (current, False), False
            metadata = dict(current.metadata)
            metadata.update(
                _queued_record_sync_metadata(
                    target_state,
                    token=expected_token,
                    owner_pid=owner_pid,
                )
            )
            updated = replace(current, metadata=metadata)
            entries[index] = updated
            return (updated, True), True
        raise RuntimeError(
            f"queue entry disappeared while publishing queued state: {entry.queue_id}"
        )

    updated, transitioned = mutate_entries(submission.queue_root, transition)
    _set_state_entry(state, updated)
    return transitioned


def _owns_queued_record_publication(
    state: _InternalEngineSubmissionState,
    *,
    expected_token: str,
    expected_state: str,
) -> bool:
    """Revalidate the queue-side fencing token before any external write."""
    submission = state.submission
    entry = state.entry
    if submission is None or not isinstance(entry, QueueEntry):
        return True

    def inspect(entries: list[QueueEntry]) -> tuple[tuple[QueueEntry, bool], bool]:
        for current in entries:
            if current.queue_id != entry.queue_id:
                continue
            if not _same_queue_generation(current, entry):
                return (current, False), False
            current_sync_state = normalize_text(current.metadata.get(QUEUE_RECORD_SYNC_KEY)).lower()
            current_token = normalize_text(current.metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY))
            owns_publication = (
                current.status == QueueStatus.PENDING
                and not current.cancel_requested
                and current_sync_state == expected_state
                and current_token == expected_token
            )
            return (current, owns_publication), False
        raise RuntimeError(
            f"queue entry disappeared while validating queued-state publication: {entry.queue_id}"
        )

    current, owns_publication = mutate_entries(submission.queue_root, inspect)
    _set_state_entry(state, current)
    return owns_publication


def _publication_was_cancelled(state: _InternalEngineSubmissionState) -> bool:
    entry = state.entry
    return isinstance(entry, QueueEntry) and (
        entry.cancel_requested or entry.status == QueueStatus.CANCELLED
    )


def _mark_queued_record_repair_pending(
    state: _InternalEngineSubmissionState,
    *,
    expected_token: str,
    expected_state: str,
) -> None:
    try:
        _transition_owned_queued_record_sync(
            state,
            expected_token=expected_token,
            expected_state=expected_state,
            target_state=QUEUE_RECORD_SYNC_REPAIR_PENDING,
            owner_pid=0,
        )
    except BaseException:  # noqa: BLE001
        logger.warning(
            "failed to mark queued record repair pending: queue_id=%s",
            getattr(state.entry, "queue_id", ""),
            exc_info=True,
        )


def _recover_post_commit_enqueue(
    state: _InternalEngineSubmissionState,
    *,
    enqueue_metadata: dict[str, Any],
) -> QueueEntry | None:
    """Fence an enqueue that may have committed before reporting an error.

    A queue save can become durable and still raise from a later durability
    barrier.  In that case the caller never receives the new ``QueueEntry`` and
    cannot use its queue id for the normal publication rollback.  Recover only
    the single row carrying every identity of this exact enqueue attempt; the
    publication token is necessary but deliberately not sufficient by itself.
    """

    submission = state.submission
    if submission is None:
        return None

    expected_app = normalize_text(getattr(submission, "app_name", ""))
    expected_task = normalize_text(getattr(submission, "task_id", ""))
    expected_kind = normalize_text(getattr(submission, "task_kind", ""))
    expected_engine = normalize_text(getattr(submission, "engine", ""))
    try:
        expected_priority = int(getattr(submission, "priority", 0))
    except (TypeError, ValueError):
        return None
    expected_token = normalize_text(enqueue_metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY))
    expected_owner_start = normalize_text(enqueue_metadata.get(QUEUE_RECORD_SYNC_OWNER_START_KEY))
    try:
        expected_owner_pid = int(enqueue_metadata.get(QUEUE_RECORD_SYNC_OWNER_PID_KEY, 0) or 0)
    except (TypeError, ValueError):
        return None
    expected_job_dir = normalize_text(enqueue_metadata.get("job_dir"))
    if (
        not all(
            (
                expected_app,
                expected_task,
                expected_kind,
                expected_engine,
                expected_token,
                expected_job_dir,
            )
        )
        or expected_owner_pid <= 0
    ):
        return None
    expected_job_dir = str(Path(expected_job_dir).expanduser().resolve())

    def identity_matches(
        current: QueueEntry,
        *,
        sync_state: str,
        owner_pid: int,
        owner_start: str,
    ) -> bool:
        metadata = current.metadata if isinstance(current.metadata, dict) else {}
        try:
            current_owner_pid = int(metadata.get(QUEUE_RECORD_SYNC_OWNER_PID_KEY, 0) or 0)
            current_priority = int(current.priority)
        except (TypeError, ValueError):
            return False
        current_job_dir = normalize_text(metadata.get("job_dir"))
        if current_job_dir:
            current_job_dir = str(Path(current_job_dir).expanduser().resolve())
        return bool(
            current.status == QueueStatus.PENDING
            and not current.cancel_requested
            and normalize_text(current.app_name) == expected_app
            and normalize_text(current.task_id) == expected_task
            and normalize_text(current.task_kind) == expected_kind
            and normalize_text(current.engine) == expected_engine
            and current_priority == expected_priority
            and normalize_text(metadata.get(QUEUE_RECORD_SYNC_KEY)).lower() == sync_state
            and normalize_text(metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY)) == expected_token
            and current_owner_pid == owner_pid
            and normalize_text(metadata.get(QUEUE_RECORD_SYNC_OWNER_START_KEY)) == owner_start
            and current_job_dir == expected_job_dir
        )

    def unique_match(
        entries: list[QueueEntry],
        *,
        sync_state: str,
        owner_pid: int,
        owner_start: str,
    ) -> tuple[int, QueueEntry] | None:
        matches = [
            (index, current)
            for index, current in enumerate(entries)
            if identity_matches(
                current,
                sync_state=sync_state,
                owner_pid=owner_pid,
                owner_start=owner_start,
            )
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise RuntimeError(
                "ambiguous persisted queue entries after enqueue error; refusing recovery"
            )
        return matches[0]

    def recover(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        matches = [
            (index, current)
            for index, current in enumerate(entries)
            if identity_matches(
                current,
                sync_state=QUEUE_RECORD_SYNC_PREPARING,
                owner_pid=expected_owner_pid,
                owner_start=expected_owner_start,
            )
        ]
        if not matches:
            return None, False
        if len(matches) != 1:
            finished_at = now_utc_iso()
            for index, current in matches:
                metadata = dict(current.metadata)
                metadata.update(
                    _queued_record_sync_metadata(
                        QUEUE_RECORD_SYNC_ABORTED,
                        token="",
                        owner_pid=0,
                    )
                )
                entries[index] = replace(
                    current,
                    status=QueueStatus.CANCELLED,
                    cancel_requested=True,
                    finished_at=finished_at,
                    metadata=metadata,
                )
            return None, True
        match = matches[0]
        index, current = match
        metadata = dict(current.metadata)
        metadata.update(
            _queued_record_sync_metadata(
                QUEUE_RECORD_SYNC_REPAIR_PENDING,
                token=expected_token,
                owner_pid=0,
            )
        )
        updated = replace(current, metadata=metadata)
        entries[index] = updated
        return updated, True

    def load_visible_recovery(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        match = unique_match(
            entries,
            sync_state=QUEUE_RECORD_SYNC_REPAIR_PENDING,
            owner_pid=0,
            owner_start="",
        )
        return (match[1] if match is not None else None), False

    recovery_error: BaseException | None = None
    try:
        recovered = mutate_entries(submission.queue_root, recover)
    except BaseException as exc:  # noqa: BLE001
        recovery_error = exc
        try:
            recovered = mutate_entries(submission.queue_root, load_visible_recovery)
        except BaseException as reload_exc:  # noqa: BLE001
            if not isinstance(reload_exc, Exception):
                raise
            recovered = None
        if not isinstance(exc, Exception):
            if recovered is not None:
                _set_state_entry(state, recovered)
            raise
    if recovered is None:
        if recovery_error is not None:
            logger.warning(
                "failed to recover a possibly committed queue enqueue: app=%s task_id=%s",
                expected_app,
                expected_task,
                exc_info=(
                    type(recovery_error),
                    recovery_error,
                    recovery_error.__traceback__,
                ),
            )
            raise _EnqueueOwnershipUnknown(
                "queue enqueue ownership is indeterminate after recovery failure"
            ) from recovery_error
        return None
    if recovery_error is not None:
        logger.warning(
            "queue enqueue recovery became visible after a durability error: app=%s task_id=%s",
            expected_app,
            expected_task,
            exc_info=(
                type(recovery_error),
                recovery_error,
                recovery_error.__traceback__,
            ),
        )
    _set_state_entry(state, recovered)
    return recovered


def _notification_failure_warning(state: _InternalEngineSubmissionState) -> None:
    _append_warning(
        state,
        "queued notification delivery failed; state/index recorded and notification "
        "was not retried (at-most-once delivery)",
    )


def _record_queued_with_warning(
    *,
    cfg: Any,
    state: _InternalEngineSubmissionState,
    record_queued_fn: Callable[[Any, Any, Any], Any],
) -> None:
    publication_token = state.publication_token
    submission = state.submission
    entry = state.entry
    if submission is None or entry is None:
        return
    repair_marked = False
    try:
        with queue_record_publication_lock(
            submission.queue_root,
            getattr(entry, "queue_id", ""),
        ):
            if not _owns_queued_record_publication(
                state,
                expected_token=publication_token,
                expected_state=QUEUE_RECORD_SYNC_PREPARING,
            ):
                if _publication_was_cancelled(state):
                    state.deferred_reason = STATUS_CANCEL_REQUESTED
                else:
                    _append_warning(
                        state,
                        "queued job record publication ownership changed; "
                        "worker/replay will reconcile",
                    )
                return
            try:
                notification_delivered = record_queued_fn(cfg, state.submission, state.entry)
                if notification_delivered is False:
                    _notification_failure_warning(state)
                if not _transition_owned_queued_record_sync(
                    state,
                    expected_token=publication_token,
                    expected_state=QUEUE_RECORD_SYNC_PREPARING,
                    target_state=QUEUE_RECORD_SYNC_COMPLETE,
                    owner_pid=0,
                ):
                    _append_warning(
                        state,
                        "queued job record publication ownership changed; "
                        "worker/replay will reconcile",
                    )
            except BaseException:  # noqa: BLE001
                _mark_queued_record_repair_pending(
                    state,
                    expected_token=publication_token,
                    expected_state=QUEUE_RECORD_SYNC_PREPARING,
                )
                repair_marked = True
                raise
    except BaseException as exc:  # noqa: BLE001
        if not repair_marked:
            _mark_queued_record_repair_pending(
                state,
                expected_token=publication_token,
                expected_state=QUEUE_RECORD_SYNC_PREPARING,
            )
        if not isinstance(exc, Exception):
            raise
        logger.warning(
            "queued job record update failed after queue submission succeeded: "
            "job_dir=%s queue_id=%s error=%s",
            state.resolved_job_dir,
            getattr(state.entry, "queue_id", ""),
            exc,
            exc_info=True,
        )
        _append_warning(
            state,
            "queued job record update failed; queue submission succeeded "
            f"({exc.__class__.__name__}: {exc})",
        )


def _repair_queued_record(
    *,
    cfg: Any,
    state: _InternalEngineSubmissionState,
    record_queued_fn: Callable[[Any, Any, Any], Any],
    suppress_queued_notification: bool = True,
) -> None:
    submission = state.submission
    entry = state.entry
    if submission is None or not isinstance(entry, QueueEntry):
        return

    repair_token = timestamped_token("record_sync", token_bytes=16)

    def claim(entries: list[QueueEntry]) -> tuple[tuple[str, QueueEntry], bool]:
        for index, current in enumerate(entries):
            if current.queue_id != entry.queue_id:
                continue
            if not _same_queue_generation(current, entry):
                return ("identity_changed", current), False
            if current.cancel_requested:
                return ("cancel_requested", current), False
            sync_state = normalize_text(current.metadata.get(QUEUE_RECORD_SYNC_KEY)).lower()
            if sync_state == QUEUE_RECORD_SYNC_COMPLETE:
                return ("complete", current), False
            if current.status == QueueStatus.RUNNING:
                return ("running", current), False
            if current.status != QueueStatus.PENDING:
                return ("terminal", current), False
            if sync_state and sync_state not in {
                QUEUE_RECORD_SYNC_PREPARING,
                QUEUE_RECORD_SYNC_REPAIR_PENDING,
                QUEUE_RECORD_SYNC_REPAIRING,
            }:
                return ("invalid_state", current), False
            metadata = dict(current.metadata)
            metadata.update(
                _queued_record_sync_metadata(
                    QUEUE_RECORD_SYNC_REPAIRING,
                    token=repair_token,
                    owner_pid=os.getpid(),
                )
            )
            updated = replace(current, metadata=metadata)
            entries[index] = updated
            return ("claimed", updated), True
        raise RuntimeError(
            f"queue entry disappeared while claiming queued-state repair: {entry.queue_id}"
        )

    try:
        # The publication lock, rather than a live PID recorded in the row, is
        # the authoritative proof of an active publication scope. Once this
        # lock is acquired an abandoned PREPARING/REPAIRING lease is safe to
        # reclaim even when its publisher is a still-running bot/API process.
        with queue_record_publication_lock(submission.queue_root, entry.queue_id):
            outcome, current = mutate_entries(submission.queue_root, claim)
    except BaseException as exc:  # noqa: BLE001
        # The queue mutation may have committed the REPAIRING lease before its
        # durability barrier reported failure.  Fence it back to retryable
        # state even for process-control exceptions, while preserving those
        # exceptions for the caller.
        _mark_queued_record_repair_pending(
            state,
            expected_token=repair_token,
            expected_state=QUEUE_RECORD_SYNC_REPAIRING,
        )
        if not isinstance(exc, Exception):
            raise
        logger.warning(
            "queued job record repair claim failed after queue submission succeeded: "
            "job_dir=%s queue_id=%s error=%s",
            state.resolved_job_dir,
            entry.queue_id,
            exc,
            exc_info=True,
        )
        _append_warning(
            state,
            "queued job record repair claim failed; queue submission succeeded "
            f"({exc.__class__.__name__}: {exc})",
        )
        return

    if outcome != "claimed":
        _set_state_entry(state, current)
        state.submission = _submission_for_existing_entry(
            submission,
            current,
            suppress_queued_notification=suppress_queued_notification,
        )
        if outcome == "cancel_requested":
            state.deferred_reason = STATUS_CANCEL_REQUESTED
        elif outcome == "invalid_state":
            raise RuntimeError(
                "queued job state/index repair refused an invalid publication state "
                f"for queue_id={current.queue_id}"
            )
        elif outcome == "running":
            _append_warning(
                state,
                "queued job state/index repair skipped because the worker is running",
            )
        elif outcome == "in_progress":
            _append_warning(state, "queued job state/index publication is already in progress")
        elif outcome == "identity_changed":
            _append_warning(
                state,
                "queued job state/index repair refused a changed queue generation",
            )
        return

    # From the moment claim commits, every subsequent operation belongs to the
    # repair lease and must be covered by the rollback fence, including local
    # reconstruction of the replay submission.
    state.publication_token = repair_token
    repair_marked = False
    try:
        _set_state_entry(state, current)
        state.submission = _submission_for_existing_entry(
            submission,
            current,
            suppress_queued_notification=suppress_queued_notification,
        )
        with queue_record_publication_lock(submission.queue_root, entry.queue_id):
            if not _owns_queued_record_publication(
                state,
                expected_token=repair_token,
                expected_state=QUEUE_RECORD_SYNC_REPAIRING,
            ):
                if _publication_was_cancelled(state):
                    state.deferred_reason = STATUS_CANCEL_REQUESTED
                else:
                    _append_warning(
                        state,
                        "queued job state/index repair ownership changed before publication",
                    )
                return
            try:
                notification_delivered = record_queued_fn(cfg, state.submission, state.entry)
                if notification_delivered is False:
                    _notification_failure_warning(state)
                if not _transition_owned_queued_record_sync(
                    state,
                    expected_token=repair_token,
                    expected_state=QUEUE_RECORD_SYNC_REPAIRING,
                    target_state=QUEUE_RECORD_SYNC_COMPLETE,
                    owner_pid=0,
                ):
                    _append_warning(
                        state,
                        "queued job state/index repaired but publication ownership changed",
                    )
                    return
            except BaseException:  # noqa: BLE001
                _mark_queued_record_repair_pending(
                    state,
                    expected_token=repair_token,
                    expected_state=QUEUE_RECORD_SYNC_REPAIRING,
                )
                repair_marked = True
                raise
    except BaseException as exc:  # noqa: BLE001
        if not repair_marked:
            _mark_queued_record_repair_pending(
                state,
                expected_token=repair_token,
                expected_state=QUEUE_RECORD_SYNC_REPAIRING,
            )
        if not isinstance(exc, Exception):
            raise
        logger.warning(
            "queued job record repair failed after queue submission succeeded: "
            "job_dir=%s queue_id=%s error=%s",
            state.resolved_job_dir,
            entry.queue_id,
            exc,
            exc_info=True,
        )
        _append_warning(
            state,
            "queued job record repair failed; queue submission succeeded "
            f"({exc.__class__.__name__}: {exc})",
        )
        return
    _append_warning(state, "queued job state/index repaired")


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
        publication_token=normalize_text(entry.metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY)),
    )
    _repair_queued_record(
        cfg=cfg,
        state=state,
        record_queued_fn=record_queued_fn,
        suppress_queued_notification=True,
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
    state.publication_token = timestamped_token("record_sync", token_bytes=16)
    publication_metadata = _queued_record_sync_metadata(
        QUEUE_RECORD_SYNC_PREPARING,
        token=state.publication_token,
        owner_pid=os.getpid(),
    )
    state.submission.metadata.update(publication_metadata)
    enqueue_metadata = dict(state.submission.metadata)
    try:
        state.entry = enqueue_fn(
            state.submission.queue_root,
            app_name=state.submission.app_name,
            task_id=state.submission.task_id,
            task_kind=state.submission.task_kind,
            engine=state.submission.engine,
            priority=state.submission.priority,
            metadata=enqueue_metadata,
            duplicate_policy=_reject_active_job_dir_duplicate,
        )
    except _ActiveJobDirCancellationPending as pending:
        _cleanup_unowned_submission_snapshot(state.submission)
        state.entry = pending.existing
        state.submission = _submission_for_existing_entry(state.submission, pending.existing)
        state.deferred_reason = STATUS_CANCEL_REQUESTED
        return
    except _ActiveJobDirReplay as replay:
        if not _matches_submission_engine_identity(replay.existing, state.submission):
            _cleanup_unowned_submission_snapshot(state.submission)
            raise RuntimeError(
                "active job directory is owned by a conflicting queue identity "
                f"(queue_id={replay.existing.queue_id})"
            ) from replay
        _cleanup_unowned_submission_snapshot(state.submission)
        state.entry = replay.existing
        state.submission = _submission_for_existing_entry(state.submission, replay.existing)
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
    except BaseException as exc:  # noqa: BLE001
        recovered = _recover_post_commit_enqueue(
            state,
            enqueue_metadata=enqueue_metadata,
        )
        if recovered is None:
            _cleanup_unowned_submission_snapshot(state.submission)
            raise
        _append_warning(
            state,
            "queue enqueue reported an error after durable persistence; "
            f"recovered queued entry ({exc.__class__.__name__}: {exc})",
        )
        if not isinstance(exc, Exception):
            raise
        _repair_queued_record(
            cfg=cfg,
            state=state,
            record_queued_fn=record_queued_fn,
            suppress_queued_notification=False,
        )
        return
    if isinstance(state.entry, QueueEntry):
        _set_state_entry(state, state.entry)
    _record_queued_with_warning(
        cfg=cfg,
        state=state,
        record_queued_fn=record_queued_fn,
    )


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
