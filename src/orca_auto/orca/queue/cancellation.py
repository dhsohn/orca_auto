from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from orca_auto.core.statuses import STATUS_RUNNING

from . import replay
from .entries import queue_entry_reaction_dir
from .models import OrcaRunningJob
from .terminal_replay import terminal_replay_marker_from_entry

if TYPE_CHECKING:
    from .worker import OrcaQueueWorker

logger = logging.getLogger(__name__)


def cancel_running_job(worker: OrcaQueueWorker, queue_id: str, job: OrcaRunningJob) -> bool:
    queue_root = replay.job_queue_root(job)
    logger.info("Cancelling running job: %s", queue_id)
    try:
        terminated = replay.terminate_process(job.process)
        if terminated is True and job.process.poll() is not None:
            job.terminal_finalize_pending = True
            replay.recover_slot_engine_process(worker.admission_root, job.admission_token)
    except Exception:
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
            job.admission_token,
        )
        return False
    try:
        current = replay.queue_entry_by_id(queue_root, queue_id)
        if current is None or not replay.mark_cancelled(
            queue_root,
            queue_id,
            expected_entry=current,
            expected_task_id=job.task_id or None,
        ):
            return False
    except Exception:
        logger.exception(
            "Failed to durably mark cancelled ORCA job %s; retaining retry ownership",
            queue_id,
        )
        return False
    terminal_entry = replay.queue_entry_by_id(queue_root, queue_id)
    if replay.normalized_entry_status(terminal_entry) == STATUS_RUNNING:
        logger.error(
            "Cancellation returned without a durable terminal queue transition: %s",
            queue_id,
        )
        return False
    marker = (
        terminal_replay_marker_from_entry(terminal_entry) if terminal_entry is not None else None
    )
    if marker is None:
        # Another owner may already have completed and cleared this exact
        # generation while the stale cancellation snapshot was in flight.
        replay.release_terminal_job(worker, job)
        return True
    assert terminal_entry is not None
    reaction_dir = queue_entry_reaction_dir(terminal_entry)
    reaction_key = replay.reaction_generation_key(terminal_entry)
    if not reaction_dir or not reaction_key:
        logger.error("Durable cancellation marker has no reaction identity: %s", queue_id)
        return False
    replay_item = replay.new_terminal_replay_work_item(
        queue_root,
        terminal_entry,
        reaction_dir=reaction_dir,
        reaction_key=reaction_key,
    )
    release_slot = False
    try:
        replay.strictly_finish_terminal_replay(worker, job, replay_item)
        release_slot = True
        return True
    except Exception:
        logger.exception(
            "Failed to finish durable cancellation replay for %s; retaining retry ownership",
            queue_id,
        )
        return False
    finally:
        if release_slot:
            replay.release_terminal_job(worker, job)


__all__ = ["cancel_running_job"]
