"""ORCA queue-worker composition under external supervision.

``OrcaQueueWorker`` owns ORCA cancellation, shutdown, recovery and its replay
state. Its common base owns process supervision, admission and the PID-file
lifecycle. The replay engine in ``queue/replay.py`` is called with explicit
state and never reaches back into the worker.
"""

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
    update_slot_metadata,
)
from orca_auto.core.config.schema import resolved_admission_limit
from orca_auto.core.engine_catalog import get_engine_catalog_entry
from orca_auto.core.queue.deferral import queue_entry_admission_deferral_reason
from orca_auto.core.queue.dependencies import ChildQueueWorkerDeps
from orca_auto.core.queue.processes import ManagedProcess
from orca_auto.core.queue.store import QueueLockTimeoutError
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.queue.worker import (
    PidFileChildProcessQueueWorker,
    start_background_process,
    terminate_process_group,
)
from orca_auto.core.queue.worker.models import ReservedQueueEntry, ReserveStatus
from orca_auto.core.statuses import STATUS_PENDING, STATUS_RUNNING
from orca_auto.orca.worker_execution import (
    BackgroundRunJobProcess,
    build_worker_child_command,
)

from ..config import AppConfig
from ..engine import ENGINE_RUNTIME
from . import publication_repair, replay, worker_tracking
from .adapter import (
    cancel_requested_ids,
    get_cancel_requested,
    mark_cancelled,
    mark_failed,
    queue_entry_app_name,
    queue_entry_id,
    queue_entry_metadata,
    queue_entry_reaction_dir,
    queue_entry_task_id,
    requeue_running_entry,
    worker_log_path,
)
from .entries import queue_entry_is_retired_workflow_owned
from .models import OrcaRunningJob, OrcaWorkerReplayState
from .terminal_replay import terminal_replay_marker_from_entry

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
        self.replay_state = OrcaWorkerReplayState()

    # -- loop lifecycle -----------------------------------------------------

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

    def _reconcile_worker_state(self) -> None:
        super()._reconcile_worker_state()
        replay.reconcile_worker_state(
            self.cfg,
            admission_root=self.admission_root,
            replay_state=self.replay_state,
        )

    # -- admission ----------------------------------------------------------

    def _reserve_next_entry(self) -> tuple[ReserveStatus, ReservedQueueEntry[QueueEntry] | None]:
        # A failed terminal side effect retains its completed job and durable marker.
        # Until it is replayed, no new generation may start in that reaction
        # directory: max_concurrent > 1 could otherwise start a forced successor in
        # another slot. The row filter withholds exactly those rows; unrelated jobs
        # are admitted. Only a generation that cannot be tied to a directory pauses
        # admission altogether.
        withheld = self._unresolved_terminal_reaction_keys()
        if withheld is None:
            logger.warning(
                "Queue admission paused: an unpublished ORCA terminal generation "
                "cannot be tied to a reaction directory"
            )
            return "blocked", None
        replay_state = self.replay_state
        if withheld != replay_state.admission_withheld_keys:
            if withheld:
                logger.warning(
                    "ORCA admission withheld for reaction dir(s) until terminal replay completes: %s",
                    ", ".join(sorted(withheld)),
                )
            else:
                logger.info("ORCA admission is no longer withheld for any reaction dir")
            replay_state.admission_withheld_keys = withheld
        if not publication_repair.repair_queue_publications(self.cfg):
            logger.warning("Queue admission paused until ORCA queued publication repair succeeds")
            return "blocked", None
        return super()._reserve_next_entry()

    def _unresolved_terminal_reaction_keys(self) -> frozenset[str] | None:
        """Reaction directories whose last generation has unpublished terminal state.

        A new generation must not start in any of them. ``None`` means one such
        generation cannot be tied to a directory, so nothing may be admitted.
        """
        items = list(self.replay_state.pending_replays.values())
        reaction_dirs: list[str] = []
        for _queue_id, job in self._running_jobs():
            job_item = job.pending_terminal_replay
            if job_item is not None:
                items.append(job_item)
            elif job.terminal_finalize_pending:
                reaction_dirs.append(str(getattr(job, "reaction_dir", "") or ""))
        keys: set[str] = set()
        for item in items:
            if not item.reaction_key:
                return None
            # The item's key was resolved when the item was built. Candidate rows
            # are resolved now, so a path retargeted since then must match too.
            keys.add(item.reaction_key)
            reaction_dirs.append(item.reaction_dir)
        for reaction_dir in reaction_dirs:
            try:
                key = replay.reaction_key_for_dir(reaction_dir)
            except (OSError, RuntimeError):
                return None
            if not key:
                return None
            keys.add(key)
        return frozenset(keys)

    def _entry_waits_for_terminal_replay(self, entry: QueueEntry) -> bool:
        """Whether claiming *entry* would start a generation in a withheld directory."""
        withheld = self.replay_state.admission_withheld_keys
        if not withheld:
            return False
        try:
            key = replay.reaction_generation_key(entry)
        except (OSError, RuntimeError):
            return True
        # A row that cannot be tied to a directory is not provably unrelated.
        return key is None or key in withheld

    def _skip_entry(self, entry: QueueEntry) -> bool:
        return (
            super()._skip_entry(entry)
            or queue_entry_is_retired_workflow_owned(entry, self.cfg.runtime.allowed_root)
            or self._entry_waits_for_terminal_replay(entry)
        )

    # -- child start --------------------------------------------------------

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

    def _handle_worker_start_error(
        self,
        queue_root: Path,
        entry: QueueEntry,
        admission_token: str,
        exc: OSError,
    ) -> None:
        logger.error("Failed to start job %s: %s", queue_entry_id(entry), exc)
        self._mark_entry_failed_and_release(
            queue_root,
            entry,
            admission_token,
            error=str(exc),
            mark_failed_fn=mark_failed,
        )

    def _on_worker_process_started(
        self,
        queue_root: Path,
        entry: QueueEntry,
        *,
        process: ManagedProcess,
        admission_token: str,
    ) -> bool:
        queue_id = queue_entry_id(entry)
        metadata = queue_entry_metadata(entry)
        work_dir = next(
            (
                value
                for key in ("job_dir", "reaction_dir")
                if (value := str(metadata.get(key, "") or "").strip())
            ),
            None,
        )
        attached = update_slot_metadata(
            self.admission_root,
            admission_token,
            state="active",
            queue_id=queue_id,
            app_name=queue_entry_app_name(entry),
            task_id=queue_entry_task_id(entry),
            owner_pid=process.pid,
            work_dir=work_dir,
        )
        if not attached:
            logger.error(
                "Failed to attach queue identity to admission slot %s for job %s",
                admission_token,
                queue_id,
            )
            terminate_process_group(process)
            self._mark_entry_failed_and_release(
                queue_root,
                entry,
                admission_token,
                error="admission_slot_missing",
                mark_failed_fn=mark_failed,
            )
            return False
        try:
            worker_tracking.upsert_running_job_record(self.cfg, entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to update running job location for %s: %s", queue_id, exc)
        return True

    # -- terminal finalization ---------------------------------------------

    def _release_terminal_job(self, job: OrcaRunningJob) -> None:
        """Release capacity before clearing the state needed to retry a failed release."""
        self._release_admission_slot(job.admission_token)
        job.pending_terminal_replay = None
        job.terminal_finalize_pending = False

    def _finalize_completed_job(self, queue_id: str, job: OrcaRunningJob, rc: int) -> None:
        # A child can exit while its engine process is still recorded as active.  Do
        # not publish a terminal queue state (or make the capacity reusable) until
        # that identity has been recovered.  Raising here deliberately leaves the
        # completed job in ``_running`` so the worker retries the whole finalization.
        job.terminal_finalize_pending = True
        recover_slot_engine_process(self.admission_root, job.admission_token)
        pending_item = job.pending_terminal_replay
        if pending_item is not None:
            release_slot_after_finalize = False
            try:
                replay.strictly_finish_terminal_replay(self.cfg, job, pending_item)
                release_slot_after_finalize = True
            finally:
                if release_slot_after_finalize:
                    self._release_terminal_job(job)
            return

        mark_result = replay.mark_terminal_queue_entry(queue_id, job, rc=rc)
        release_slot_after_finalize = False
        try:
            # A no-op is benign only when another actor already moved or removed the
            # queue row. The mark result carries a pre-mark snapshot, so re-read before
            # deciding: an actually RUNNING row has no durable terminal owner and must
            # retain the job and slot for the supervised completion retry.
            current_after_mark = replay.queue_entry_by_id(mark_result.queue_root, queue_id)
            if replay.normalized_entry_status(current_after_mark) == STATUS_RUNNING:
                raise RuntimeError(
                    "terminal queue mark did not update the running entry; "
                    f"retaining retry ownership for {queue_id}"
                )
            deferral_reason = (
                queue_entry_admission_deferral_reason(current_after_mark)
                if replay.normalized_entry_status(current_after_mark) == STATUS_PENDING
                else ""
            )
            if deferral_reason:
                logger.warning(
                    "ORCA job %s was not started and waits in the queue: %s",
                    queue_id,
                    deferral_reason,
                )
            marker = (
                terminal_replay_marker_from_entry(current_after_mark)
                if current_after_mark is not None
                else None
            )
            if marker is not None:
                assert current_after_mark is not None
                reaction_dir = queue_entry_reaction_dir(current_after_mark)
                reaction_key = replay.reaction_generation_key(current_after_mark)
                if not reaction_dir or not reaction_key:
                    raise RuntimeError(
                        f"terminal replay marker has no durable reaction identity: {queue_id}"
                    )
                item = replay.new_terminal_replay_work_item(
                    mark_result.queue_root,
                    current_after_mark,
                    reaction_dir=reaction_dir,
                    reaction_key=reaction_key,
                )
                replay.strictly_finish_terminal_replay(self.cfg, job, item)
            elif mark_result.marked:
                logger.info(
                    "Terminal queue generation was already closed before finalizer replay: %s",
                    queue_id,
                )
            release_slot_after_finalize = True
        finally:
            if release_slot_after_finalize:
                self._release_terminal_job(job)

    # -- cancellation -------------------------------------------------------

    def _check_cancel_requests(self) -> None:
        jobs_by_root: dict[Path, list[tuple[str, OrcaRunningJob]]] = {}
        for queue_id, job in self._running_jobs():
            # Reaped children retained for completion retry must never be signalled.
            if job.process.poll() is not None:
                continue
            root = replay.job_queue_root(job)
            jobs_by_root.setdefault(root, []).append((queue_id, job))
        for root, jobs in jobs_by_root.items():
            expected_tasks = {
                queue_id: getattr(job, "task_id", None) or None for queue_id, job in jobs
            }
            try:
                requested = cancel_requested_ids(root, expected_tasks)
            except QueueLockTimeoutError:
                # Retry next pass; other queue roots still get their cancellation pass.
                continue
            for queue_id, job in jobs:
                if queue_id in requested and job.process.poll() is None:
                    if self._cancel_running_job(queue_id, job) is True:
                        self._discard_running_job(queue_id)

    def _cancel_running_job(self, queue_id: str, job: OrcaRunningJob) -> bool:
        """Stop *job*, mark its row cancelled and finish the durable cancellation replay.

        Returns ``True`` only when the job's queue and admission ownership has
        been fully released; otherwise the job is retained for retry.
        """
        queue_root = replay.job_queue_root(job)
        logger.info("Cancelling running job: %s", queue_id)
        try:
            terminated = terminate_process_group(job.process)
            if terminated is True and job.process.poll() is not None:
                job.terminal_finalize_pending = True
                recover_slot_engine_process(self.admission_root, job.admission_token)
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
            if current is None or not mark_cancelled(
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
            terminal_replay_marker_from_entry(terminal_entry)
            if terminal_entry is not None
            else None
        )
        if marker is None:
            # Another owner may already have completed and cleared this exact
            # generation while the stale cancellation snapshot was in flight.
            self._release_terminal_job(job)
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
        release_slot_after_replay = False
        try:
            replay.strictly_finish_terminal_replay(self.cfg, job, replay_item)
            release_slot_after_replay = True
            return True
        except Exception:
            logger.exception(
                "Failed to finish durable cancellation replay for %s; retaining retry ownership",
                queue_id,
            )
            return False
        finally:
            if release_slot_after_replay:
                self._release_terminal_job(job)

    # -- shutdown -----------------------------------------------------------

    def _before_shutdown_all(self, running_count: int) -> None:
        logger.info("Shutting down %d running job(s)...", running_count)

    def _shutdown_running_job(self, queue_id: str, job: OrcaRunningJob) -> None:
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
            self._cancel_running_job(queue_id, job)
            return

        terminated = terminate_process_group(job.process)
        if terminated is True and job.process.poll() is not None:
            recover_slot_engine_process(self.admission_root, job.admission_token)
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
                self._finalize_completed_job(queue_id, job, exit_code)
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
            self._release_admission_slot(job.admission_token)
