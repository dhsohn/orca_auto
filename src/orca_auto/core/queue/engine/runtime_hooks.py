from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import lifecycle as _queue_lifecycle
from ..worker import PidFileChildProcessQueueWorkerHooks, engine_queue_worker_source
from .admission import attach_started_process


def attach_started_child_process(
    *,
    engine: str,
    worker: Any,
    queue_root: Path,
    entry: Any,
    process: Any,
    admission_token: str,
    activate_reserved_slot_fn: Callable[..., Any],
    terminate_process_fn: Callable[[Any], Any],
    mark_failed_fn: Callable[..., Any],
) -> bool:
    return attach_started_process(
        admission_root=worker.admission_root,
        queue_root=queue_root,
        entry=entry,
        process=process,
        admission_token=admission_token,
        activate_reserved_slot_fn=activate_reserved_slot_fn,
        terminate_process_fn=terminate_process_fn,
        mark_entry_failed_and_release_fn=worker._mark_entry_failed_and_release,
        mark_failed_fn=mark_failed_fn,
        source=f"{engine_queue_worker_source(engine)}.child",
    )


def shutdown_child_job(
    worker: Any,
    job: Any,
    *,
    terminate_process_fn: Callable[[Any], Any],
    finalize_child_exit_fn: Callable[..., Any],
    grace_seconds: float,
    sleep_fn: Callable[[float], None],
) -> bool:
    return _queue_lifecycle.shutdown_running_job(
        job,
        terminate_process_fn=terminate_process_fn,
        finalize_child_exit_fn=lambda current_job, rc: finalize_child_exit_fn(
            worker,
            current_job,
            rc=rc,
        ),
        grace_seconds=grace_seconds,
        sleep_fn=sleep_fn,
    )


def build_child_worker_hooks(
    *,
    engine: str,
    handle_worker_start_error_fn: Callable[[Any, Path, Any, str, OSError], None],
    finalize_completed_job_fn: Callable[[Any, str, Any, int], None],
    finalize_child_exit_fn: Callable[..., Any],
    reconcile_worker_state_fn: Callable[[Any], None],
    activate_reserved_slot_fn: Callable[..., Any],
    terminate_process_fn: Callable[[Any], Any],
    mark_failed_fn: Callable[..., Any],
    shutdown_grace_seconds: float,
    sleep_fn: Callable[[float], None],
    on_worker_process_started_fn: Callable[[Any, Path, Any, Any, str], bool] | None = None,
    shutdown_running_job_fn: Callable[[Any, str, Any], Any] | None = None,
    before_shutdown_all_fn: Callable[[Any, int], Any] | None = None,
) -> PidFileChildProcessQueueWorkerHooks:
    on_worker_process_started = on_worker_process_started_fn or (
        lambda worker, queue_root, entry, process, admission_token: attach_started_child_process(
            engine=engine,
            worker=worker,
            queue_root=queue_root,
            entry=entry,
            process=process,
            admission_token=admission_token,
            activate_reserved_slot_fn=activate_reserved_slot_fn,
            terminate_process_fn=terminate_process_fn,
            mark_failed_fn=mark_failed_fn,
        )
    )

    def shutdown_running_job(worker: Any, queue_id: str, job: Any) -> None:
        if shutdown_running_job_fn is not None:
            shutdown_running_job_fn(worker, queue_id, job)
            return
        shutdown_child_job(
            worker,
            job,
            terminate_process_fn=terminate_process_fn,
            finalize_child_exit_fn=finalize_child_exit_fn,
            grace_seconds=shutdown_grace_seconds,
            sleep_fn=sleep_fn,
        )

    return PidFileChildProcessQueueWorkerHooks(
        handle_worker_start_error=handle_worker_start_error_fn,
        on_worker_process_started=on_worker_process_started,
        finalize_completed_job=finalize_completed_job_fn,
        shutdown_running_job=shutdown_running_job,
        reconcile_worker_state=reconcile_worker_state_fn,
        before_shutdown_all=before_shutdown_all_fn,
    )


__all__ = [
    "attach_started_child_process",
    "build_child_worker_hooks",
    "shutdown_child_job",
]
