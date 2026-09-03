from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChildExitPolicy:
    shutdown_requested: bool = True
    fail_unexpected_exit: bool = False
    use_entry_fallback: bool = True
    recovery_entry_fn: Callable[[Any, Any], Any] | None = None


@dataclass(frozen=True)
class OrphanedRunningPolicy:
    recovery_reason: str = "crashed_recovery"


@dataclass(frozen=True)
class EngineQueueTerminalSideEffectHooks:
    upsert_terminal_job_record_fn: Callable[..., Any]
    notify_terminal_job_from_state_fn: Callable[..., bool]


@dataclass(frozen=True)
class EngineQueueProcessLifecycleHooks:
    queue_entry_id_fn: Callable[[Any], str]
    queue_entry_app_name_fn: Callable[[Any], str]
    queue_entry_task_id_fn: Callable[[Any], str | None]
    update_slot_metadata_fn: Callable[..., Any]
    terminate_process_fn: Callable[[Any], bool]
    mark_failed_fn: Callable[..., Any]
    upsert_running_job_record_fn: Callable[[Any, Any], Any]
    get_run_id_from_state_fn: Callable[..., str | None]
    get_cancel_requested_fn: Callable[..., bool]
    mark_cancelled_fn: Callable[..., Any]
    mark_completed_fn: Callable[..., Any]
    upsert_terminal_job_record_fn: Callable[..., Any]
    notify_terminal_job_from_state_fn: Callable[..., bool]
    find_queue_entry_fn: Callable[[Any, str], Any | None] | None = None
    on_completed_fn: Callable[[Any, Any], Any] | None = None
    terminal_side_effect_hooks: EngineQueueTerminalSideEffectHooks | None = None


@dataclass(frozen=True)
class EngineQueueProcessReconcileHooks:
    queue_roots_fn: Callable[[Any], tuple[Any, ...]]
    reconcile_stale_slots_fn: Callable[[Any], Any]
    reconcile_orphaned_running_entries_fn: Callable[..., Any]
    reconcile_orphaned_running_entries_kwargs: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class EngineQueueProcessShutdownHooks:
    terminate_process_fn: Callable[[Any], Any]
    requeue_running_entry_fn: Callable[..., Any]
    # Optional ``(worker, queue_id, job, rc)`` completion path for a child that
    # exited with a non-negative code by the time termination returned: it had
    # reached its own conclusion (completed, failed, or requeued itself), so
    # requeueing it would re-execute finished work. Engines that leave it None
    # keep the unconditional requeue.
    finalize_completed_fn: Callable[[Any, str, Any, int], None] | None = None
    # Optional ``(worker, queue_id, job) -> bool`` guard for the completion
    # path: True when the child's run reached a terminal conclusion or its row
    # is no longer running. A non-negative exit with a still-running row and a
    # non-terminal run state (a child that crashed while handling the stop)
    # keeps the requeue/resume path instead of being recorded as a failure.
    child_concluded_fn: Callable[[Any, str, Any], bool] | None = None


__all__ = [
    "ChildExitPolicy",
    "EngineQueueProcessLifecycleHooks",
    "EngineQueueProcessReconcileHooks",
    "EngineQueueProcessShutdownHooks",
    "EngineQueueTerminalSideEffectHooks",
    "OrphanedRunningPolicy",
]
