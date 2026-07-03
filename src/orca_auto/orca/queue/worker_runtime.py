from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.queue.worker import EngineRunningJob

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
    running_job_cls: type[EngineRunningJob] = EngineRunningJob,
) -> EngineRunningJob:
    running = running_job_cls(
        queue_id=queue_entry_id_fn(entry),
        reaction_dir=queue_entry_reaction_dir_fn(entry),
        task_id=queue_entry_task_id_fn(entry) or None,
        process=process,
        admission_token=admission_token,
    )
    running.__dict__["queue_root"] = queue_root
    return running


def check_cancel_requests(
    worker: Any,
    *,
    get_cancel_requested_fn: Callable[[Path, str], bool],
    job_queue_root_fn: Callable[[Any, Any], Path],
    cancel_running_job_fn: Callable[[Any, str, Any], Any],
) -> None:
    for queue_id, job in worker._running_jobs():
        if get_cancel_requested_fn(job_queue_root_fn(worker, job), queue_id):
            cancel_running_job_fn(worker, queue_id, job)
            worker._discard_running_job(queue_id)


def install_worker_runtime_methods(
    worker: Any,
    *,
    cancel_running_job_fn: Callable[[Any, str, Any], Any],
) -> None:
    worker.__dict__["_cancel_running_job"] = lambda queue_id, job: cancel_running_job_fn(
        worker, queue_id, job
    )


__all__ = [
    "after_worker_run",
    "before_worker_run",
    "check_cancel_requests",
    "install_worker_runtime_methods",
    "log_worker_interrupt",
    "make_running_job",
]
