from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.queue.store import QueueLockTimeoutError

from .models import OrcaRunningJob

logger = logging.getLogger(__name__)


def before_worker_run(worker: Any) -> None:
    logger.info(
        "Queue worker started (pid=%d, max_concurrent=%d, admission_root=%s, admission_limit=%d)",
        os.getpid(),
        worker.max_concurrent,
        worker.admission_root,
        worker.admission_limit,
    )


def after_worker_run(_worker: Any) -> None:
    logger.info("Queue worker stopped")


def log_worker_interrupt(_worker: Any) -> None:
    logger.info("Queue worker interrupted")


def make_running_job(
    *,
    queue_root: Path,
    entry: Any,
    process: Any,
    admission_token: str,
    queue_entry_id_fn: Callable[[Any], str],
    queue_entry_reaction_dir_fn: Callable[[Any], str],
    queue_entry_task_id_fn: Callable[[Any], str | None],
) -> OrcaRunningJob:
    return OrcaRunningJob(
        queue_root=queue_root,
        queue_id=queue_entry_id_fn(entry),
        reaction_dir=queue_entry_reaction_dir_fn(entry),
        task_id=queue_entry_task_id_fn(entry) or None,
        process=process,
        admission_token=admission_token,
    )


def check_cancel_requests(
    worker: Any,
    *,
    cancel_requested_ids_fn: Callable[[Path, Mapping[str, str | None]], set[str]],
    job_queue_root_fn: Callable[[Any, Any], Path],
    cancel_running_job_fn: Callable[[Any, str, Any], bool],
) -> None:
    jobs_by_root: dict[Path, list[tuple[str, Any]]] = {}
    for queue_id, job in worker._running_jobs():
        # Reaped children retained for completion retry must never be signalled.
        if job.process.poll() is not None:
            continue
        root = job_queue_root_fn(worker, job)
        jobs_by_root.setdefault(root, []).append((queue_id, job))
    for root, jobs in jobs_by_root.items():
        expected_tasks = {queue_id: getattr(job, "task_id", None) or None for queue_id, job in jobs}
        try:
            requested = cancel_requested_ids_fn(root, expected_tasks)
        except QueueLockTimeoutError:
            # Retry next pass; other queue roots still get their cancellation pass.
            continue
        for queue_id, job in jobs:
            if queue_id in requested and job.process.poll() is None:
                if cancel_running_job_fn(worker, queue_id, job) is True:
                    worker._discard_running_job(queue_id)


__all__ = [
    "after_worker_run",
    "before_worker_run",
    "check_cancel_requests",
    "log_worker_interrupt",
    "make_running_job",
]
