from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RunInpStatusDeps:
    AdmissionLimitReachedError: Any
    AnalyzerStatus: Any
    RunStatus: Any


@dataclass(frozen=True)
class RunInpExecutionDeps:
    acquire_run_lock: Any
    load_or_create_state: Any
    release_slot: Any
    run_attempts: Any
    save_state: Any
    _admission_context: Any
    _emit: Any
    _execute_locked_run: Any
    _existing_completed_exit: Any
    _existing_completed_out: Any
    _exit_with_result: Any
    _notification_callbacks: Any
    _recover_crashed_state: Any
    _release_reservation_if_needed: Any
    _resolve_execution_context: Any
    _retry_inp_path: Any
    _run_with_state: Any
    _to_resolved_local: Any


@dataclass(frozen=True)
class RunInpNotificationDeps:
    notify_queue_enqueued_event: Any
    notify_retry_event: Any
    notify_run_finished_event: Any
    notify_run_started_event: Any


@dataclass(frozen=True)
class RunInpSubmissionDeps:
    ensure_submission_resource_request: Any
    prepare_submission_resource_request: Any
    read_resource_request_from_input: Any
    active_direct_run_error: Any
    emit_queued_submission: Any
    queue_adapter: Any
    resolve_submission_context: Any
    select_latest_inp: Any
    submit_reaction_dir_to_queue: Any


@dataclass(frozen=True)
class RunInpDeps:
    statuses: RunInpStatusDeps
    execution: RunInpExecutionDeps
    notifications: RunInpNotificationDeps
    submission: RunInpSubmissionDeps


__all__ = [
    "RunInpDeps",
    "RunInpExecutionDeps",
    "RunInpNotificationDeps",
    "RunInpStatusDeps",
    "RunInpSubmissionDeps",
]
