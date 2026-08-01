from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from orca_auto.core.statuses import STATUS_CANCEL_REQUESTED

from ..child.process import requeue_result_is_cancelled, shutdown_child_process_with_grace
from .hooks import (
    ChildExitPolicy,
    EngineQueueProcessLifecycleHooks,
    EngineQueueProcessShutdownHooks,
)
from .terminal import entry_status_is_running, job_queue_root

LOGGER = logging.getLogger("orca_auto.core.queue.lifecycle")


def cancel_running_process_job(
    worker: Any,
    queue_id: str,
    job: Any,
    *,
    hooks: EngineQueueProcessLifecycleHooks,
    release_admission_slot: bool = True,
    logger: logging.Logger = LOGGER,
) -> bool:
    """Stop and mark a running job, optionally deferring admission release.

    Callers that must durably finalize engine-specific state before making the
    capacity reusable can pass ``release_admission_slot=False`` and release the
    job's token in their own outer ``finally`` block.
    """

    logger.info("Cancelling running job: %s", queue_id)
    try:
        terminated = hooks.terminate_process_fn(job.process)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to terminate running job %s; retaining queue and admission ownership",
            queue_id,
        )
        return False
    try:
        process_exited = job.process.poll() is not None
    except Exception:  # noqa: BLE001
        process_exited = False
    if terminated is not True or not process_exited:
        logger.error(
            "Running job %s did not fully stop; retaining queue entry and admission slot %s",
            queue_id,
            getattr(job, "admission_token", ""),
        )
        return False
    generation_kwargs = {}
    if getattr(job, "task_id", None):
        generation_kwargs["expected_task_id"] = job.task_id
    if not release_admission_slot:
        mark_outcome = hooks.mark_cancelled_fn(
            job_queue_root(worker, job),
            queue_id,
            **generation_kwargs,
        )
        return mark_outcome is not False
    try:
        hooks.mark_cancelled_fn(
            job_queue_root(worker, job),
            queue_id,
            **generation_kwargs,
        )
    finally:
        worker._release_admission_slot(job.admission_token)
    return True


def shutdown_running_process_job(
    worker: Any,
    queue_id: str,
    job: Any,
    *,
    hooks: EngineQueueProcessShutdownHooks,
    logger: logging.Logger = LOGGER,
) -> None:
    terminated = hooks.terminate_process_fn(job.process)
    if terminated is not True:
        logger.error(
            "Process for running job %s did not stop; leaving queue entry running "
            "and retaining admission slot %s",
            queue_id,
            getattr(job, "admission_token", ""),
        )
        return
    try:
        generation_kwargs = {}
        if getattr(job, "task_id", None):
            generation_kwargs["expected_task_id"] = job.task_id
        hooks.requeue_running_entry_fn(
            job_queue_root(worker, job),
            queue_id,
            **generation_kwargs,
        )
    finally:
        worker._release_admission_slot(job.admission_token)


def shutdown_running_job(
    job: Any,
    *,
    finalize_child_exit_fn: Callable[[Any, int], Any],
    grace_seconds: float,
    sleep_fn: Callable[[float], None],
    shutdown_child_process_with_grace_fn: Callable[..., Any] = shutdown_child_process_with_grace,
) -> bool:
    return bool(
        shutdown_child_process_with_grace_fn(
            job,
            finalize_child_exit_fn=lambda current_job, rc: finalize_child_exit_fn(
                current_job,
                rc,
            ),
            grace_seconds=grace_seconds,
            sleep_fn=sleep_fn,
        )
    )


def finalize_child_worker_exit(
    cfg: Any,
    job: Any,
    *,
    find_queue_entry_fn: Callable[[Any, str], Any | None],
    mark_cancelled_fn: Callable[..., Any],
    requeue_running_entry_fn: Callable[..., Any],
    mark_recovery_pending_fn: Callable[..., Any],
    release_admission_slot_fn: Callable[[str], Any],
    mark_failed_fn: Callable[..., Any] | None = None,
    rc: int | None = None,
    shutdown_requested: bool = True,
    fail_unexpected_exit: bool = False,
    use_entry_fallback: bool = True,
    recovery_entry_fn: Callable[[Any, Any], Any] | None = None,
) -> None:
    root = job.queue_root
    current = find_queue_entry_fn(job.queue_root, job.entry.queue_id)
    if current is None and use_entry_fallback:
        current = job.entry

    job_task_id = getattr(job.entry, "task_id", None)
    current_task_id = getattr(current, "task_id", None) if current is not None else None
    same_generation = not (
        job_task_id and current_task_id and str(job_task_id).strip() != str(current_task_id).strip()
    )
    generation_kwargs = {"expected_entry": current}
    if job_task_id:
        generation_kwargs["expected_task_id"] = job_task_id

    try:
        if current is not None and same_generation and entry_status_is_running(current):
            queue_id = current.queue_id
            if getattr(current, "cancel_requested", False):
                mark_cancelled_fn(
                    root,
                    queue_id,
                    error=STATUS_CANCEL_REQUESTED,
                    **generation_kwargs,
                )
            elif shutdown_requested:
                updated = requeue_running_entry_fn(root, queue_id, **generation_kwargs)
                if updated is None or requeue_result_is_cancelled(updated):
                    return
                recovery_entry = (
                    recovery_entry_fn(current, job) if recovery_entry_fn is not None else current
                )
                mark_recovery_pending_fn(cfg, recovery_entry, reason="worker_shutdown")
            elif fail_unexpected_exit and mark_failed_fn is not None:
                mark_failed_fn(
                    root,
                    queue_id,
                    error=f"worker_child_exit_code={int(rc or 0)}",
                    **generation_kwargs,
                )
    finally:
        release_admission_slot_fn(job.admission_token)


def finalize_child_exit_with_policy(
    cfg: Any,
    job: Any,
    *,
    policy: ChildExitPolicy,
    find_queue_entry_fn: Callable[[Any, str], Any | None],
    mark_cancelled_fn: Callable[..., Any],
    requeue_running_entry_fn: Callable[..., Any],
    mark_recovery_pending_fn: Callable[..., Any],
    release_admission_slot_fn: Callable[[str], Any],
    mark_failed_fn: Callable[..., Any] | None = None,
    rc: int | None = None,
) -> None:
    finalize_child_worker_exit(
        cfg,
        job,
        find_queue_entry_fn=find_queue_entry_fn,
        mark_cancelled_fn=mark_cancelled_fn,
        requeue_running_entry_fn=requeue_running_entry_fn,
        mark_recovery_pending_fn=mark_recovery_pending_fn,
        release_admission_slot_fn=release_admission_slot_fn,
        mark_failed_fn=mark_failed_fn,
        rc=rc,
        shutdown_requested=policy.shutdown_requested,
        fail_unexpected_exit=policy.fail_unexpected_exit,
        use_entry_fallback=policy.use_entry_fallback,
        recovery_entry_fn=policy.recovery_entry_fn,
    )


def request_pending_cancellations(
    running_jobs: Iterable[tuple[str, Any]],
    *,
    get_cancel_requested_fn: Callable[..., bool],
    request_job_cancellation_fn: Callable[[Any], Any],
) -> None:
    for _queue_id, job in running_jobs:
        if job.cancel_requested:
            continue
        generation_kwargs = {"expected_entry": job.entry}
        task_id = getattr(job.entry, "task_id", None)
        if task_id:
            generation_kwargs["expected_task_id"] = task_id
        if get_cancel_requested_fn(
            str(job.queue_root),
            job.entry.queue_id,
            **generation_kwargs,
        ):
            request_job_cancellation_fn(job.process)
            job.cancel_requested = True


__all__ = [
    "cancel_running_process_job",
    "finalize_child_exit_with_policy",
    "finalize_child_worker_exit",
    "request_pending_cancellations",
    "shutdown_running_job",
    "shutdown_running_process_job",
]
