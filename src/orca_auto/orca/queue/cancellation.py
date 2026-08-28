from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from orca_auto.core.engines.queue_worker import EngineQueueWorker
from orca_auto.core.queue.lifecycle import cancel_running_process_job
from orca_auto.core.queue.worker import EngineRunningJob
from orca_auto.core.statuses import STATUS_RUNNING

from . import replay
from .entries import queue_entry_reaction_dir
from .terminal_replay import terminal_replay_marker_from_entry

logger = logging.getLogger(__name__)


def cancel_running_job(worker: EngineQueueWorker, queue_id: str, job: EngineRunningJob) -> bool:
    hooks = replay.orca_worker_lifecycle_hooks()
    queue_root = replay.job_queue_root(worker, job)

    def terminate_child_and_recover(process: Any) -> bool:
        terminated = hooks.terminate_process_fn(process)
        if terminated is True and process.poll() is not None:
            job.__dict__[replay.TERMINAL_FINALIZE_RETRY_ATTR] = True
            replay.recover_slot_engine_process(worker.admission_root, job.admission_token)
        return terminated

    try:
        cancelled = cancel_running_process_job(
            worker,
            queue_id,
            job,
            hooks=replace(
                hooks,
                terminate_process_fn=terminate_child_and_recover,
            ),
            release_admission_slot=False,
        )
    except Exception:
        logger.exception(
            "Failed to durably mark cancelled ORCA job %s; retaining retry ownership",
            queue_id,
        )
        return False
    if not cancelled:
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
        worker._release_admission_slot(job.admission_token)
        job.__dict__.pop("_orca_terminal_replay_item", None)
        job.__dict__.pop(replay.TERMINAL_FINALIZE_RETRY_ATTR, None)
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
            worker._release_admission_slot(job.admission_token)
            job.__dict__.pop("_orca_terminal_replay_item", None)
            job.__dict__.pop(replay.TERMINAL_FINALIZE_RETRY_ATTR, None)


__all__ = ["cancel_running_job"]
