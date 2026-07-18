from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .. import submission

logger = logging.getLogger(__name__)


def _emit_queued_submission(
    reaction_dir: Path,
    entry: Any,
    *,
    worker_status: str | None,
    worker_pid: int | None,
    worker_log: str | Path | None,
    worker_detail: str | None = None,
) -> None:
    print("status: queued")
    print(f"job_dir: {reaction_dir}")
    print(f"queue_id: {submission.queue_adapter.queue_entry_id(entry)}")
    task_id = submission.queue_adapter.queue_entry_task_id(entry)
    if task_id:
        print(f"job_id: {task_id}")
    print(f"priority: {submission.queue_adapter.queue_entry_priority(entry)}")
    if submission.queue_adapter.queue_entry_force(entry):
        print("force: true")
    if worker_status:
        print(f"worker: {worker_status}")
    if worker_pid is not None:
        print(f"worker_pid: {worker_pid}")
    if worker_log:
        print(f"worker_log: {worker_log}")
    if worker_detail:
        print(f"worker_detail: {worker_detail}")


def cmd_run_inp(args: Any) -> int:
    result = submission.submit_reaction_dir_to_queue(args)
    if result.status != "submitted":
        if result.stderr:
            logger.error("%s", result.stderr.rstrip())
        return 1

    queued = result.queued_result
    context = result.context
    if queued is None or context is None:
        logger.error("ORCA queue submission did not return a queued result.")
        return 1
    worker_info = queued.worker_info
    _emit_queued_submission(
        context.reaction_dir,
        queued.entry,
        worker_status=worker_info.status,
        worker_pid=worker_info.pid,
        worker_log=worker_info.log_file,
        worker_detail=worker_info.detail,
    )
    return 0
