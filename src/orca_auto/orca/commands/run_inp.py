from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .. import submission

logger = logging.getLogger(__name__)


def _queued_submission_payload(
    reaction_dir: Path,
    entry: Any,
    *,
    worker_status: str | None,
    worker_pid: int | None,
    worker_log: str | Path | None,
    worker_detail: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "queued",
        "job_dir": str(reaction_dir),
        "queue_id": submission.queue_adapter.queue_entry_id(entry),
    }
    task_id = submission.queue_adapter.queue_entry_task_id(entry)
    if task_id:
        payload["job_id"] = task_id
    payload["priority"] = submission.queue_adapter.queue_entry_priority(entry)
    if submission.queue_adapter.queue_entry_force(entry):
        payload["force"] = True
    if worker_status:
        payload["worker"] = worker_status
    if worker_pid is not None:
        payload["worker_pid"] = worker_pid
    if worker_log:
        payload["worker_log"] = str(worker_log)
    if worker_detail:
        payload["worker_detail"] = worker_detail
    return payload


def _emit_queued_submission(
    reaction_dir: Path,
    entry: Any,
    *,
    worker_status: str | None,
    worker_pid: int | None,
    worker_log: str | Path | None,
    worker_detail: str | None = None,
    json_output: bool = False,
) -> None:
    payload = _queued_submission_payload(
        reaction_dir,
        entry,
        worker_status=worker_status,
        worker_pid=worker_pid,
        worker_log=worker_log,
        worker_detail=worker_detail,
    )
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    for key, value in payload.items():
        rendered = "true" if value is True else str(value)
        print(f"{key}: {rendered}")


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
        json_output=bool(getattr(args, "json", False)),
    )
    return 0
