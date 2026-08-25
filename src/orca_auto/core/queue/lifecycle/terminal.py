from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.statuses import TERMINAL_STATUSES

from ..child.process import status_matches
from ..engine import admission as _engine_admission
from ..types import QueueStatus
from .hooks import (
    EngineQueueProcessLifecycleHooks,
    EngineQueueTerminalSideEffectHooks,
)

LOGGER = logging.getLogger("orca_auto.core.queue.lifecycle")


@dataclass(frozen=True)
class TerminalProcessQueueMarkResult:
    """Stable context captured while deciding and applying a terminal queue mark."""

    marked: bool
    status: str | None
    expected_job_id: str | None
    current_entry: Any | None
    queue_root: Path
    run_id: str | None = None


def entry_status_is(entry: Any, expected: Any) -> bool:
    return status_matches(getattr(entry, "status", None), expected)


def entry_status_is_running(entry: Any) -> bool:
    return entry_status_is(entry, QueueStatus.RUNNING)


def job_queue_root(worker: Any, job: Any) -> Path:
    return Path(getattr(job, "queue_root", worker.allowed_root)).expanduser().resolve()


def attach_started_process_metadata(
    worker: Any,
    queue_root: Any,
    entry: Any,
    *,
    process: Any,
    admission_token: str,
    hooks: EngineQueueProcessLifecycleHooks,
    logger: logging.Logger = LOGGER,
) -> bool:
    return _engine_admission.attach_started_process_metadata(
        admission_root=worker.admission_root,
        queue_root=queue_root,
        entry=entry,
        process=process,
        admission_token=admission_token,
        queue_entry_id_fn=hooks.queue_entry_id_fn,
        queue_entry_app_name_fn=hooks.queue_entry_app_name_fn,
        queue_entry_task_id_fn=hooks.queue_entry_task_id_fn,
        update_slot_metadata_fn=hooks.update_slot_metadata_fn,
        terminate_process_fn=hooks.terminate_process_fn,
        mark_entry_failed_and_release_fn=worker._mark_entry_failed_and_release,
        mark_failed_fn=hooks.mark_failed_fn,
        cfg=worker.cfg,
        upsert_running_job_record_fn=hooks.upsert_running_job_record_fn,
        logger=logger,
    )


def mark_terminal_process_queue_entry_with_result(
    worker: Any,
    queue_id: str,
    job: Any,
    *,
    rc: int,
    hooks: EngineQueueProcessLifecycleHooks,
    logger: logging.Logger = LOGGER,
) -> TerminalProcessQueueMarkResult:
    queue_root = job_queue_root(worker, job)
    current = None
    job_task_id = getattr(job, "task_id", None)
    if hooks.find_queue_entry_fn is not None:
        current = hooks.find_queue_entry_fn(queue_root, queue_id)
        current_task_id = hooks.queue_entry_task_id_fn(current) if current is not None else None
        expected_job_id = current_task_id or job_task_id
        if current is None or not entry_status_is_running(current):
            logger.info(
                "Skipping terminal mark for %s; entry is no longer running",
                queue_id,
            )
            return TerminalProcessQueueMarkResult(
                marked=False,
                status=None,
                expected_job_id=expected_job_id,
                current_entry=current,
                queue_root=queue_root,
            )
        if job_task_id and current_task_id and job_task_id != current_task_id:
            logger.error(
                "Skipping terminal mark for %s; running queue generation changed",
                queue_id,
            )
            return TerminalProcessQueueMarkResult(
                marked=False,
                status=None,
                expected_job_id=job_task_id,
                current_entry=current,
                queue_root=queue_root,
            )
    else:
        expected_job_id = job_task_id
    if expected_job_id:
        run_id = hooks.get_run_id_from_state_fn(
            job.reaction_dir,
            expected_job_id=expected_job_id,
        )
    else:
        run_id = hooks.get_run_id_from_state_fn(job.reaction_dir)
    generation_kwargs = {
        "expected_entry": current,
        "expected_task_id": expected_job_id,
    }
    if hooks.get_cancel_requested_fn(queue_root, queue_id, **generation_kwargs):
        status = QueueStatus.CANCELLED.value
        logger.info("Job cancelled: %s (rc=%d)", queue_id, rc)
        mark_outcome = hooks.mark_cancelled_fn(queue_root, queue_id, **generation_kwargs)
    elif rc == 0:
        status = QueueStatus.COMPLETED.value
        logger.info("Job completed: %s (rc=%d)", queue_id, rc)
        mark_outcome = hooks.mark_completed_fn(
            queue_root,
            queue_id,
            run_id=run_id,
            **generation_kwargs,
        )
    else:
        status = QueueStatus.FAILED.value
        logger.warning("Job failed: %s (rc=%d)", queue_id, rc)
        mark_outcome = hooks.mark_failed_fn(
            queue_root,
            queue_id,
            error=f"exit_code={rc}",
            run_id=run_id,
            **generation_kwargs,
        )
    if mark_outcome is False:
        logger.info(
            "Skipping terminal finalization for %s; terminal mark did not update the entry",
            queue_id,
        )
        return TerminalProcessQueueMarkResult(
            marked=False,
            status=None,
            expected_job_id=expected_job_id,
            current_entry=current,
            queue_root=queue_root,
            run_id=run_id,
        )
    return TerminalProcessQueueMarkResult(
        marked=True,
        status=status,
        expected_job_id=expected_job_id,
        current_entry=current,
        queue_root=queue_root,
        run_id=run_id,
    )


def run_terminal_process_side_effects(
    worker: Any,
    queue_id: str,
    job: Any,
    *,
    hooks: EngineQueueTerminalSideEffectHooks,
    expected_job_id: str | None = None,
    logger: logging.Logger = LOGGER,
) -> None:
    expected_job_id = expected_job_id or getattr(job, "task_id", None)
    try:
        hooks.upsert_terminal_job_record_fn(
            worker.cfg,
            job.reaction_dir,
            fallback_job_id=expected_job_id,
            expected_job_id=expected_job_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to update terminal job location for %s: %s", queue_id, exc)
    try:
        hooks.notify_terminal_job_from_state_fn(
            worker.cfg,
            job.reaction_dir,
            expected_job_id=expected_job_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to send terminal notification for %s: %s", queue_id, exc)


def record_terminal_process_side_effects(
    worker: Any,
    queue_id: str,
    job: Any,
    *,
    rc: int,
    hooks: EngineQueueProcessLifecycleHooks,
    expected_job_id: str | None = None,
    logger: logging.Logger = LOGGER,
) -> None:
    if rc == 0 and hooks.on_completed_fn is not None:
        try:
            hooks.on_completed_fn(worker, job)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to run completed-job side effect for %s: %s", queue_id, exc)
    expected_job_id = expected_job_id or getattr(job, "task_id", None)
    if not expected_job_id and hooks.find_queue_entry_fn is not None:
        current = hooks.find_queue_entry_fn(job_queue_root(worker, job), queue_id)
        if current is not None:
            expected_job_id = hooks.queue_entry_task_id_fn(current)
    run_terminal_process_side_effects(
        worker,
        queue_id,
        job,
        hooks=hooks.terminal_side_effect_hooks
        or EngineQueueTerminalSideEffectHooks(
            upsert_terminal_job_record_fn=hooks.upsert_terminal_job_record_fn,
            notify_terminal_job_from_state_fn=hooks.notify_terminal_job_from_state_fn,
        ),
        expected_job_id=expected_job_id,
        logger=logger,
    )


def sync_terminal_running_entries(
    queue_entries: Iterable[tuple[Any, Any]],
    *,
    load_terminal_summary_fn: Callable[..., Any],
    ensure_terminal_queue_status_fn: Callable[..., Any],
) -> None:
    for queue_root, entry in queue_entries:
        if not entry_status_is_running(entry):
            continue
        summary = load_terminal_summary_fn(queue_root, entry)
        if summary.status in TERMINAL_STATUSES:
            ensure_terminal_queue_status_fn(queue_root, entry, summary)


__all__ = [
    "TerminalProcessQueueMarkResult",
    "attach_started_process_metadata",
    "entry_status_is",
    "entry_status_is_running",
    "job_queue_root",
    "mark_terminal_process_queue_entry_with_result",
    "record_terminal_process_side_effects",
    "run_terminal_process_side_effects",
    "sync_terminal_running_entries",
]
