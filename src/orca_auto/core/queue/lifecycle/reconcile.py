from __future__ import annotations

from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import Any

from ..child.process import reconcile_orphaned_child_queue_entries
from ..types import QueueStatus
from .hooks import (
    EngineQueueProcessReconcileHooks,
    OrphanedRunningPolicy,
)
from .terminal import entry_status_is_running


def reconcile_orphaned_process_entries(
    worker: Any,
    *,
    hooks: EngineQueueProcessReconcileHooks,
) -> None:
    hooks.reconcile_stale_slots_fn(worker.admission_root)
    kwargs = dict(hooks.reconcile_orphaned_running_entries_kwargs or {})
    for queue_root in hooks.queue_roots_fn(worker.cfg):
        hooks.reconcile_orphaned_running_entries_fn(queue_root, **kwargs)


def live_worker_pid_slots(
    queue_entries: Iterable[tuple[Any, Any]],
    *,
    load_state_fn: Callable[[Any], dict[str, Any] | None],
    job_dir_fn: Callable[[Any], Any],
    pid_is_alive_fn: Callable[[int], bool],
) -> list[Any]:
    slots: list[Any] = []
    for _queue_root, entry in queue_entries:
        if not entry_status_is_running(entry):
            continue
        state = load_state_fn(job_dir_fn(entry)) or {}
        try:
            process_payload = state.get("process")
            worker_job_pid = int(
                (process_payload.get("worker_pid", 0) if isinstance(process_payload, dict) else 0)
                or 0
            )
        except (TypeError, ValueError):
            continue
        if worker_job_pid and pid_is_alive_fn(worker_job_pid):
            slots.append(SimpleNamespace(queue_id=entry.queue_id))
    return slots


def reconcile_orphaned_running(
    cfg: Any,
    *,
    admission_root: Any,
    queue_roots_fn: Callable[[Any], tuple[Any, ...]],
    list_queue_fn: Callable[[Any], list[Any]],
    list_slots_fn: Callable[[Any], list[Any]],
    reconcile_stale_slots_fn: Callable[[Any], Any],
    mark_cancelled_fn: Callable[..., Any],
    requeue_running_entry_fn: Callable[..., Any],
    mark_recovery_pending_fn: Callable[..., Any],
    recovery_reason: str = "crashed_recovery",
    reconcile_orphaned_child_queue_entries_fn: Callable[
        ..., Any
    ] = reconcile_orphaned_child_queue_entries,
) -> None:
    reconcile_orphaned_child_queue_entries_fn(
        cfg,
        admission_root=admission_root,
        queue_roots_fn=queue_roots_fn,
        list_queue_fn=list_queue_fn,
        list_slots_fn=list_slots_fn,
        reconcile_stale_slots_fn=reconcile_stale_slots_fn,
        running_status=QueueStatus.RUNNING,
        mark_cancelled_fn=lambda root, queue_id, **kwargs: mark_cancelled_fn(
            root,
            queue_id,
            **kwargs,
        ),
        requeue_running_entry_fn=lambda root, queue_id: requeue_running_entry_fn(
            root,
            queue_id,
        ),
        mark_recovery_pending_fn=lambda cfg_obj, entry: mark_recovery_pending_fn(
            cfg_obj,
            entry,
            reason=recovery_reason,
        ),
    )


def reconcile_orphaned_running_with_policy(
    cfg: Any,
    *,
    policy: OrphanedRunningPolicy,
    admission_root: Any,
    queue_roots_fn: Callable[[Any], tuple[Any, ...]],
    list_queue_fn: Callable[[Any], list[Any]],
    list_slots_fn: Callable[[Any], list[Any]],
    reconcile_stale_slots_fn: Callable[[Any], Any],
    mark_cancelled_fn: Callable[..., Any],
    requeue_running_entry_fn: Callable[..., Any],
    mark_recovery_pending_fn: Callable[..., Any],
    reconcile_orphaned_child_queue_entries_fn: Callable[
        ..., Any
    ] = reconcile_orphaned_child_queue_entries,
) -> None:
    reconcile_orphaned_running(
        cfg,
        admission_root=admission_root,
        queue_roots_fn=queue_roots_fn,
        list_queue_fn=list_queue_fn,
        list_slots_fn=list_slots_fn,
        reconcile_stale_slots_fn=reconcile_stale_slots_fn,
        mark_cancelled_fn=mark_cancelled_fn,
        requeue_running_entry_fn=requeue_running_entry_fn,
        mark_recovery_pending_fn=mark_recovery_pending_fn,
        recovery_reason=policy.recovery_reason,
        reconcile_orphaned_child_queue_entries_fn=reconcile_orphaned_child_queue_entries_fn,
    )


__all__ = [
    "live_worker_pid_slots",
    "reconcile_orphaned_process_entries",
    "reconcile_orphaned_running",
    "reconcile_orphaned_running_with_policy",
]
