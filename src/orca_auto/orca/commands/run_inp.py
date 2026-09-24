"""``orca_auto run-dir`` for ORCA: submit one input directory to the queue.

The command prints the queued-submission payload (text or ``--json``) and
returns 0; a refused or failed submission is logged and handed to
``report_error`` so the top-level CLI can render it on stderr regardless of
where the submission log goes. This module sits below the terminal layer and
never imports it.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.statuses import STATUS_QUEUED

from .. import submission

logger = logging.getLogger(__name__)


def _print_error_line(message: str) -> None:
    """Default ``report_error``: a plain ``error:`` line on stderr."""

    print(f"error: {message}", file=sys.stderr)


def _print_json_document(payload: Mapping[str, Any]) -> None:
    """Default ``emit_json``: one plain JSON document on stdout."""

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


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
        "status": STATUS_QUEUED,
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
    emit_json: Callable[[Mapping[str, Any]], None] = _print_json_document,
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
        emit_json(payload)
        return
    for key, value in payload.items():
        rendered = "true" if value is True else str(value)
        print(f"{key}: {rendered}")


def cmd_run_inp(
    args: Any,
    *,
    report_error: Callable[[str], None] = _print_error_line,
    emit_json: Callable[[Mapping[str, Any]], None] = _print_json_document,
) -> int:
    """Submit ``args.path``; a failure is logged and reported, then returns 1.

    The CLI layer supplies ``report_error``/``emit_json`` so the operator
    surface (stderr ``error:`` lines, the shared ``--json`` document) stays
    with the terminal module this package cannot import.
    """

    def fail(message: str) -> int:
        # The log line stays for the submission log; ``report_error`` is the
        # operator-facing line (and, under --json, the error document).
        logger.error("%s", message)
        report_error(message)
        return 1

    result = submission.submit_reaction_dir_to_queue(args)
    if result.status != "submitted":
        return fail(result.stderr.rstrip() if result.stderr else "ORCA queue submission failed.")

    queued = result.queued_result
    context = result.context
    if queued is None or context is None:
        return fail("ORCA queue submission did not return a queued result.")
    worker_info = queued.worker_info
    _emit_queued_submission(
        context.reaction_dir,
        queued.entry,
        worker_status=worker_info.status,
        worker_pid=worker_info.pid,
        worker_log=worker_info.log_file,
        worker_detail=worker_info.detail,
        json_output=bool(getattr(args, "json", False)),
        emit_json=emit_json,
    )
    return 0
