"""ORCA queue-worker composition under external supervision."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import replace
from pathlib import Path

from orca_auto.core.admission import (
    recover_slot_engine_process,
    release_slot,
    reserve_slot,
)
from orca_auto.core.config.schema import resolved_admission_limit
from orca_auto.core.engine_catalog import get_engine_catalog_entry
from orca_auto.core.queue.dependencies import ChildQueueWorkerDeps
from orca_auto.core.queue.processes import ManagedProcess
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.queue.worker import PidFileChildProcessQueueWorker, start_background_process
from orca_auto.core.queue.worker.models import ReservedQueueEntry, ReserveStatus
from orca_auto.orca.worker_execution import (
    BackgroundRunJobProcess,
    build_worker_child_command,
)

from ..config import AppConfig
from ..engine import ENGINE_RUNTIME
from . import cancellation, publication_repair, replay, worker_runtime
from .adapter import (
    get_cancel_requested,
    queue_entry_id,
    queue_entry_reaction_dir,
    queue_entry_task_id,
    requeue_running_entry,
    worker_log_path,
)
from .entries import queue_entry_is_retired_workflow_owned
from .models import OrcaRunningJob

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT = 4
POLL_INTERVAL_SECONDS = 5


def queue_roots(cfg: AppConfig) -> tuple[Path, ...]:
    return ENGINE_RUNTIME.queue_roots(cfg)


def queue_entries_with_roots(cfg: AppConfig) -> list[tuple[Path, QueueEntry]]:
    return ENGINE_RUNTIME.queue_entries_with_roots(cfg)


def _try_reserve_admission_slot(cfg: AppConfig) -> str | None:
    catalog_entry = get_engine_catalog_entry("orca")
    admission_token = reserve_slot(
        Path(cfg.runtime.resolved_admission_root),
        cfg.runtime.resolved_admission_limit,
        source=catalog_entry.admission_source,
        app_name=catalog_entry.app_id,
        state="reserved",
        engine_process_state="idle",
        engine_launch_gated=catalog_entry.engine_launch_gated,
    )
    if admission_token is None:
        logger.debug(
            "Queue worker admission paused: admission slots are full (admission_limit=%d)",
            cfg.runtime.resolved_admission_limit,
        )
    return admission_token


def _start_background_job_process(
    *,
    config_path: str,
    queue_root: Path,
    entry: QueueEntry,
    admission_token: str,
) -> BackgroundRunJobProcess[str]:
    log_path = str(worker_log_path(queue_root, queue_entry_id(entry)))
    return start_background_process(
        build_worker_child_command(
            config_path=config_path,
            queue_root=queue_root,
            queue_id=queue_entry_id(entry),
            admission_token=admission_token,
        ),
        log_path=log_path,
    )


def _worker_admission_limit(cfg: AppConfig, fallback_max_concurrent: int) -> int:
    raw_limit = cfg.runtime.admission_limit
    if raw_limit in (None, "", 0):
        raw_limit = fallback_max_concurrent
    return resolved_admission_limit(raw_limit, fallback_max_concurrent)


def _worker_config_with_effective_concurrency(
    cfg: AppConfig,
    configured_max: int,
) -> AppConfig:
    if cfg.runtime.admission_limit not in (None, "", 0):
        return cfg
    return replace(cfg, runtime=replace(cfg.runtime, max_concurrent=configured_max))


def _shutdown_running_job(worker: OrcaQueueWorker, queue_id: str, job: OrcaRunningJob) -> None:
    try:
        cancel_requested = get_cancel_requested(
            replay.job_queue_root(job),
            queue_id,
            expected_task_id=job.task_id,
        )
    except Exception:
        # The child must still be stopped. The shared requeue chokepoint below
        # honors a pending cancel on its own (it marks the row cancelled
        # instead of requeueing); only the proactive side effects are skipped,
        # and the durable marker replays them on the next start.
        logger.exception(
            "Reading the cancel flag of running job %s failed during shutdown; "
            "stopping it through the ordinary requeue path",
            queue_id,
        )
        cancel_requested = False
    if cancel_requested:
        # A cancel landed before the worker loop could process it proactively. The
        # shared requeue chokepoint would still honor it (mark the entry cancelled
        # instead of requeuing for resume) but skip the terminal side effects --
        # the cancelled run state and the notification. Route it through the same
        # finalize path as a proactive cancel so the user is told it stopped and no
        # stale "running" run state lingers.
        cancellation.cancel_running_job(worker, queue_id, job)
        return

    terminated = replay.terminate_process(job.process)
    if terminated is True and job.process.poll() is not None:
        recover_slot_engine_process(worker.admission_root, job.admission_token)
    if terminated is not True:
        logger.error(
            "Process for running job %s did not stop; leaving queue entry running "
            "and retaining admission slot %s",
            queue_id,
            job.admission_token,
        )
        return
    exit_code = job.process.poll()
    if exit_code is not None and exit_code >= 0 and replay.child_run_concluded(queue_id, job):
        # Finished work keeps its durable terminal owner if publication fails;
        # the next worker start retries it without requeueing the calculation.
        try:
            replay.finalize_completed_job(worker, queue_id, job, exit_code)
        except Exception:
            logger.exception(
                "Finalizing job %s (exit code %d) during shutdown failed; leaving its "
                "queue entry and admission slot %s for the next worker start",
                queue_id,
                exit_code,
                job.admission_token,
            )
        return
    try:
        queue_root = replay.job_queue_root(job)
        current = replay.queue_entry_by_id(queue_root, queue_id)
        if current is not None:
            requeue_running_entry(
                queue_root,
                queue_id,
                expected_entry=current,
                expected_task_id=job.task_id or None,
            )
    finally:
        worker._release_admission_slot(job.admission_token)


def _orca_reserve_gate(
    worker: OrcaQueueWorker,
) -> tuple[ReserveStatus, ReservedQueueEntry[QueueEntry] | None] | None:
    # A failed terminal side effect retains its completed job and durable marker.
    # Until it is replayed, no new generation may start in that reaction
    # directory: max_concurrent > 1 could otherwise start a forced successor in
    # another slot. The row filter withholds exactly those rows; unrelated jobs
    # are admitted. Only a generation that cannot be tied to a directory pauses
    # admission altogether.
    withheld = replay.unresolved_terminal_reaction_keys(worker)
    if withheld is None:
        logger.warning(
            "Queue admission paused: an unpublished ORCA terminal generation "
            "cannot be tied to a reaction directory"
        )
        return "blocked", None
    replay_state = worker.replay_state
    if withheld != replay_state.admission_withheld_keys:
        if withheld:
            logger.warning(
                "ORCA admission withheld for reaction dir(s) until terminal replay completes: %s",
                ", ".join(sorted(withheld)),
            )
        else:
            logger.info("ORCA admission is no longer withheld for any reaction dir")
        replay_state.admission_withheld_keys = withheld
    if not publication_repair.repair_queue_publications(worker):
        logger.warning("Queue admission paused until ORCA queued publication repair succeeds")
        return "blocked", None
    return None


class OrcaQueueWorker(PidFileChildProcessQueueWorker[AppConfig, OrcaRunningJob]):
    """Supervise ORCA children with explicit cancellation and recovery ownership."""

    worker_pid_file_name = ENGINE_RUNTIME.worker_pid_file_name

    def __init__(
        self,
        cfg: AppConfig,
        config_path: str,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        deps: ChildQueueWorkerDeps[AppConfig] | None = None,
    ) -> None:
        configured_max = max(1, int(max_concurrent))
        worker_cfg = _worker_config_with_effective_concurrency(cfg, configured_max)
        if deps is None:
            deps = ChildQueueWorkerDeps(
                poll_interval_seconds=POLL_INTERVAL_SECONDS,
                sleep=time.sleep,
                release_slot=release_slot,
                start_background_job_process=_start_background_job_process,
                has_admission_capacity=ENGINE_RUNTIME.has_admission_capacity,
                peek_next_entry=ENGINE_RUNTIME.peek_next_entry,
                dequeue_next_entry=ENGINE_RUNTIME.dequeue_next_entry,
                try_reserve_admission_slot=_try_reserve_admission_slot,
            )
        super().__init__(
            worker_cfg,
            config_path=str(config_path or "").strip(),
            max_concurrent=configured_max,
            deps=deps,
            admission_root=worker_cfg.runtime.resolved_admission_root,
        )
        self.admission_limit = _worker_admission_limit(worker_cfg, self.max_concurrent)
        self.replay_state = replay.OrcaWorkerReplayState()

    def _before_run(self) -> None:
        super()._before_run()
        logger.info(
            "Queue worker started (pid=%d, max_concurrent=%d, admission_root=%s, admission_limit=%d)",
            os.getpid(),
            self.max_concurrent,
            self.admission_root,
            self.admission_limit,
        )

    def _after_run(self) -> None:
        super()._after_run()
        logger.info("Queue worker stopped")

    def _run_iteration(self) -> None:
        try:
            super()._run_iteration()
        except KeyboardInterrupt:
            logger.info("Queue worker interrupted")
            raise

    def _reserve_next_entry(self) -> tuple[ReserveStatus, ReservedQueueEntry[QueueEntry] | None]:
        gated = _orca_reserve_gate(self)
        return gated if gated is not None else super()._reserve_next_entry()

    def _skip_entry(self, entry: QueueEntry) -> bool:
        return (
            super()._skip_entry(entry)
            or queue_entry_is_retired_workflow_owned(entry, self.cfg.runtime.allowed_root)
            or replay.entry_waits_for_terminal_replay(self, entry)
        )

    def _running_queue_id(self, entry: QueueEntry) -> str:
        return queue_entry_id(entry)

    def _make_running_job(
        self,
        *,
        queue_root: Path,
        entry: QueueEntry,
        process: ManagedProcess,
        admission_token: str,
    ) -> OrcaRunningJob:
        return OrcaRunningJob(
            queue_root=queue_root,
            queue_id=queue_entry_id(entry),
            reaction_dir=queue_entry_reaction_dir(entry),
            task_id=queue_entry_task_id(entry) or None,
            process=process,
            admission_token=admission_token,
        )

    def _check_cancel_requests(self) -> None:
        worker_runtime.check_cancel_requests(self)

    def _handle_worker_start_error(
        self,
        queue_root: Path,
        entry: QueueEntry,
        admission_token: str,
        exc: OSError,
    ) -> None:
        replay.handle_worker_start_error(self, queue_root, entry, admission_token, exc)

    def _on_worker_process_started(
        self,
        queue_root: Path,
        entry: QueueEntry,
        *,
        process: ManagedProcess,
        admission_token: str,
    ) -> bool:
        return replay.on_worker_process_started(self, queue_root, entry, process, admission_token)

    def _finalize_completed_job(self, queue_id: str, job: OrcaRunningJob, rc: int) -> None:
        replay.finalize_completed_job(self, queue_id, job, rc)

    def _before_shutdown_all(self, running_count: int) -> None:
        logger.info("Shutting down %d running job(s)...", running_count)

    def _shutdown_running_job(self, queue_id: str, job: OrcaRunningJob) -> None:
        _shutdown_running_job(self, queue_id, job)

    def _reconcile_worker_state(self) -> None:
        super()._reconcile_worker_state()
        replay.reconcile_worker_state(self)
