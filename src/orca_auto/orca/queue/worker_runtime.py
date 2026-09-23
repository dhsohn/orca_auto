from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from orca_auto.core.queue.store import QueueLockTimeoutError

from . import cancellation, replay
from .adapter import cancel_requested_ids
from .models import OrcaRunningJob

if TYPE_CHECKING:
    from .worker import OrcaQueueWorker

logger = logging.getLogger(__name__)


def check_cancel_requests(worker: OrcaQueueWorker) -> None:
    jobs_by_root: dict[Path, list[tuple[str, OrcaRunningJob]]] = {}
    for queue_id, job in worker._running_jobs():
        # Reaped children retained for completion retry must never be signalled.
        if job.process.poll() is not None:
            continue
        root = replay.job_queue_root(job)
        jobs_by_root.setdefault(root, []).append((queue_id, job))
    for root, jobs in jobs_by_root.items():
        expected_tasks = {queue_id: getattr(job, "task_id", None) or None for queue_id, job in jobs}
        try:
            requested = cancel_requested_ids(root, expected_tasks)
        except QueueLockTimeoutError:
            # Retry next pass; other queue roots still get their cancellation pass.
            continue
        for queue_id, job in jobs:
            if queue_id in requested and job.process.poll() is None:
                if cancellation.cancel_running_job(worker, queue_id, job) is True:
                    worker._discard_running_job(queue_id)


__all__ = ["check_cancel_requests"]
