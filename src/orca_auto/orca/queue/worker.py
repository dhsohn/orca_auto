"""ORCA queue-worker composition under external supervision.

``OrcaQueueWorker`` is the one queue worker: it owns the PID-file lifecycle,
admission (slot reserved before the row is claimed), child start and attach,
terminal finalization, cancellation, shutdown and recovery. The poll loop it
inherits from ``core.queue.worker.loop`` only orders the passes. The replay
engine in ``queue/replay.py`` is called with explicit state and never reaches
back into the worker.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from orca_auto.core.admission import (
    recover_slot_engine_process,
    release_slot,
    reserve_slot,
    update_slot_metadata,
)
from orca_auto.core.config.schema import resolved_admission_limit
from orca_auto.core.queue.deferral import queue_entry_admission_deferral_reason
from orca_auto.core.queue.engine.snapshot_intent import (
    finalize_queued_snapshot_intent,
    reconcile_orphaned_snapshot_generations,
    snapshot_runtime_roots_for_cfg,
)
from orca_auto.core.queue.processes import ManagedProcess
from orca_auto.core.queue.store import QueueLockTimeoutError
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.queue.worker import (
    WORKER_PID_FILE_NAME,
    QueueWorkerLoop,
    ReservedQueueEntry,
    ReserveStatus,
    admission_has_capacity,
    remove_worker_pid_file,
    reserve_dequeued_entry,
    start_background_process,
    terminate_process_group,
    worker_pid_file_path,
    write_worker_pid_file,
)
from orca_auto.core.statuses import STATUS_PENDING, STATUS_RUNNING
from orca_auto.core.utils.lock import file_lock
from orca_auto.orca.engine_catalog import get_engine_catalog_entry
from orca_auto.orca.worker_execution import build_worker_child_command

from ..config import AppConfig
from . import publication_repair, replay, roots, worker_tracking
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
from .models import OrcaRunningJob, OrcaWorkerReplayState, TerminalReplayWorkItem
from .terminal_replay import terminal_replay_marker_from_entry

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT = 4
POLL_INTERVAL_SECONDS = 5
_SNAPSHOT_INTENT_RECONCILE_INTERVAL_SECONDS = 300.0
_WORKER_STATE_RECONCILE_INTERVAL_SECONDS = 60.0


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


def _host_core_count() -> int | None:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count()


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


class OrcaQueueWorker(QueueWorkerLoop):
    """Supervise ORCA children with explicit cancellation and recovery ownership."""

    worker_pid_file_name = WORKER_PID_FILE_NAME
    worker_lock_timeout_seconds = 0.0
    _running: dict[str, OrcaRunningJob]

    def __init__(
        self,
        cfg: AppConfig,
        config_path: str,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        configured_max = max(1, int(max_concurrent))
        worker_cfg = _worker_config_with_effective_concurrency(cfg, configured_max)
        super().__init__(
            max_concurrent=configured_max,
            poll_interval_seconds=POLL_INTERVAL_SECONDS,
            sleep_fn=sleep_fn,
        )
        self.cfg = worker_cfg
        self.config_path = str(config_path or "").strip()
        self.allowed_root = Path(str(worker_cfg.runtime.allowed_root)).expanduser().resolve()
        self.admission_root = (
            Path(str(worker_cfg.runtime.resolved_admission_root)).expanduser().resolve()
        )
        self.admission_limit = _worker_admission_limit(worker_cfg, self.max_concurrent)
        self.replay_state = OrcaWorkerReplayState()
        self._worker_state_last_reconcile: float | None = None
        self._snapshot_intent_last_reconcile: float | None = None
        self._publication_withheld_ids: frozenset[str] = frozenset()

    # -- pid file and singleton lock ----------------------------------------

    def _pid_file_path(self) -> Path:
        return worker_pid_file_path(self.allowed_root, self.worker_pid_file_name)

    def _lock_file_path(self) -> Path:
        return self._pid_file_path().with_name(f"{self.worker_pid_file_name}.lock")

    def _write_pid_file(self) -> None:
        write_worker_pid_file(self.allowed_root, self.worker_pid_file_name)

    def _remove_pid_file(self) -> None:
        remove_worker_pid_file(self.allowed_root, self.worker_pid_file_name)

    def _acquire_worker_lock(self, stack: contextlib.ExitStack) -> bool:
        lock_path = self._lock_file_path()
        try:
            stack.enter_context(
                file_lock(lock_path, timeout_seconds=self.worker_lock_timeout_seconds)
            )
        except TimeoutError:
            print(
                f"error: queue worker already running (lock={lock_path})",
                file=sys.stderr,
            )
            return False
        return True

    # -- loop lifecycle -----------------------------------------------------

    def run(self) -> int:
        with contextlib.ExitStack() as stack:
            if not self._acquire_worker_lock(stack):
                return 1
            return super().run()

    def run_once(
        self,
        *,
        idle_message: str | None = "No pending jobs.",
        blocked_message: str | None = "status: waiting_for_slot",
    ) -> int:
        with contextlib.ExitStack() as stack:
            if not self._acquire_worker_lock(stack):
                return 1
            return super().run_once(
                idle_message=idle_message,
                blocked_message=blocked_message,
            )

    def _before_run(self) -> None:
        self._write_pid_file()
        self._reconcile_worker_state_now()
        logger.info(
            "Queue worker started (pid=%d, max_concurrent=%d, admission_root=%s, admission_limit=%d)",
            os.getpid(),
            self.max_concurrent,
            self.admission_root,
            self.admission_limit,
        )
        self._warn_if_concurrency_exceeds_host_cores()

    def _warn_if_concurrency_exceeds_host_cores(self) -> None:
        host_cores = _host_core_count()
        cores_per_task = int(self.cfg.resources.max_cores_per_task)
        requested = self.max_concurrent * cores_per_task
        if host_cores is None or requested <= host_cores:
            return
        logger.warning(
            "Configured concurrency can oversubscribe the host: max_concurrent=%d x "
            "max_cores_per_task=%d requests %d cores, but this worker can use %d",
            self.max_concurrent,
            cores_per_task,
            requested,
            host_cores,
        )

    def _after_run(self) -> None:
        self._remove_pid_file()
        logger.info("Queue worker stopped")

    def _run_iteration(self) -> None:
        try:
            super()._run_iteration()
        except KeyboardInterrupt:
            logger.info("Queue worker interrupted")
            raise

    def _sleep(self) -> None:
        # A replacement parent may initially observe a live child from the
        # previous parent and correctly skip it. Periodically reconcile so that,
        # once that child exits (or is killed), its queue entry/engine record is
        # recovered without repeatedly scanning every runtime root on each
        # short queue-poll cycle.
        # Do not race a completed in-memory job whose finalization is being
        # retained for retry after an error.
        completed_retry_pending = any(
            self._poll_job(job) is not None for _queue_id, job in self._running_jobs()
        )
        if not completed_retry_pending:
            self._reconcile_worker_state_if_due()
        super()._sleep()

    def _running_jobs(self) -> list[tuple[str, OrcaRunningJob]]:
        return list(self._running.items())

    # -- recovery -----------------------------------------------------------

    def _reconcile_worker_state_now(self) -> None:
        self._reconcile_worker_state()
        self._worker_state_last_reconcile = time.monotonic()

    def _reconcile_worker_state_if_due(self) -> None:
        last_reconcile = self._worker_state_last_reconcile
        if (
            last_reconcile is None
            or time.monotonic() - last_reconcile >= _WORKER_STATE_RECONCILE_INTERVAL_SECONDS
        ):
            self._reconcile_worker_state_now()

    def _reconcile_worker_state(self) -> None:
        self._reconcile_snapshot_intents_if_due()
        replay.reconcile_worker_state(
            self.cfg,
            admission_root=self.admission_root,
            replay_state=self.replay_state,
        )

    def _reconcile_snapshot_intents_if_due(self) -> None:
        now = time.monotonic()
        last_reconcile = self._snapshot_intent_last_reconcile
        if (
            last_reconcile is not None
            and now - last_reconcile < _SNAPSHOT_INTENT_RECONCILE_INTERVAL_SECONDS
        ):
            return
        self._snapshot_intent_last_reconcile = now
        try:
            removed = reconcile_orphaned_snapshot_generations(
                snapshot_runtime_roots_for_cfg(self.cfg)
            )
        except Exception:
            logger.exception("Snapshot orphan reconciliation failed; retaining all candidates")
        else:
            if removed:
                logger.info("Removed %d abandoned pre-enqueue snapshot intent(s)", removed)

    # -- admission ----------------------------------------------------------

    def _admission_has_capacity(self) -> bool:
        return admission_has_capacity(self.cfg)

    def _peek_next_entry(self) -> tuple[Path, QueueEntry] | None:
        return roots.peek_next_entry(self.cfg, skip_entry_fn=self._skip_entry)

    def _dequeue_next_entry(self) -> tuple[Path, QueueEntry] | None:
        return roots.dequeue_next_entry(self.cfg, skip_entry_fn=self._skip_entry)

    def _reserve_admission_slot(self) -> str | None:
        return _try_reserve_admission_slot(self.cfg)

    def _release_admission_slot(self, admission_token: str) -> object:
        released: object = release_slot(self.admission_root, admission_token)
        return released

    def _reserve_next_entry(self) -> tuple[ReserveStatus, ReservedQueueEntry | None]:
        # Pending publication retains its replay snapshot and durable queue marker.
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
        publication_withheld_ids = publication_repair.repair_queue_publications(self.cfg)
        if publication_withheld_ids is None:
            logger.warning("Queue admission paused: ORCA queue could not be inspected")
            return "blocked", None
        self._publication_withheld_ids = publication_withheld_ids
        return reserve_dequeued_entry(
            has_capacity_fn=self._admission_has_capacity,
            peek_next_fn=self._peek_next_entry,
            reserve_slot_fn=self._reserve_admission_slot,
            dequeue_next_fn=self._dequeue_next_entry,
            release_slot_fn=self._release_admission_slot,
        )

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
        """Rows this worker must not claim now; rows behind them stay eligible."""
        # A child can return its own row to pending and only then exit. Until
        # that exit has been finalized here, starting the row again would
        # replace the tracked job and strand its admission slot.
        return (
            queue_entry_id(entry) in self._running
            or queue_entry_id(entry) in self._publication_withheld_ids
            or queue_entry_is_retired_workflow_owned(entry, self.cfg.runtime.allowed_root)
            or self._entry_waits_for_terminal_replay(entry)
        )

    # -- child start --------------------------------------------------------

    def _start_reserved(self, reserved: ReservedQueueEntry) -> bool:
        try:
            # Retire the journal before execution so even a very fast terminal
            # job cannot lose its queue row while an ENQUEUEING intent remains.
            finalize_queued_snapshot_intent(reserved.queue_root, reserved.entry)
        except Exception as exc:  # noqa: BLE001
            self._handle_worker_start_error(
                reserved.queue_root,
                reserved.entry,
                reserved.admission_token,
                OSError(f"snapshot intent finalization failed: {exc}"),
            )
            return False
        return self._start_job(
            reserved.queue_root,
            reserved.entry,
            admission_token=reserved.admission_token,
        )

    def _start_background_process(
        self,
        *,
        queue_root: Path,
        entry: QueueEntry,
        admission_token: str,
    ) -> ManagedProcess:
        """Spawn the detached ORCA child for *entry*; tests substitute this seam."""
        log_path = str(worker_log_path(queue_root, queue_entry_id(entry)))
        return start_background_process(
            build_worker_child_command(
                config_path=self.config_path,
                queue_root=queue_root,
                queue_id=queue_entry_id(entry),
                admission_token=admission_token,
            ),
            log_path=log_path,
        )

    def _start_job(self, queue_root: Path, entry: QueueEntry, *, admission_token: str) -> bool:
        try:
            proc = self._start_background_process(
                queue_root=queue_root,
                entry=entry,
                admission_token=admission_token,
            )
        except OSError as exc:
            self._handle_worker_start_error(queue_root, entry, admission_token, exc)
            return False
        except Exception as exc:  # noqa: BLE001
            self._handle_worker_start_error(
                queue_root,
                entry,
                admission_token,
                OSError(f"worker start failed: {exc}"),
            )
            return False

        try:
            if not self._on_worker_process_started(
                queue_root,
                entry,
                process=proc,
                admission_token=admission_token,
            ):
                # The one refusal: the reserved slot vanished before attach. The
                # child must not run unadmitted; fail the row and release once.
                terminate_process_group(proc)
                self._handle_worker_start_error(
                    queue_root,
                    entry,
                    admission_token,
                    OSError("admission_slot_missing"),
                )
                return False
        except Exception as exc:  # noqa: BLE001
            self._terminate_untracked_process(proc)
            self._handle_worker_start_error(
                queue_root,
                entry,
                admission_token,
                OSError(f"worker attach failed: {exc}"),
            )
            return False

        self._running[queue_entry_id(entry)] = self._make_running_job(
            queue_root=queue_root,
            entry=entry,
            process=proc,
            admission_token=admission_token,
        )
        return True

    def _terminate_untracked_process(self, process: ManagedProcess) -> None:
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            with contextlib.suppress(Exception):
                terminate()

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
        self._mark_entry_failed_and_release(queue_root, entry, admission_token, error=str(exc))

    def _mark_entry_failed_and_release(
        self,
        queue_root: Path,
        entry: QueueEntry,
        admission_token: str,
        *,
        error: str,
    ) -> None:
        """Mark the selected generation failed, then release its slot even if the mark fails."""
        try:
            mark_failed(
                queue_root,
                queue_entry_id(entry),
                error=error,
                expected_entry=entry,
            )
        finally:
            self._release_admission_slot(admission_token)

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

    def _finish_terminal_job(self, job: OrcaRunningJob, item: TerminalReplayWorkItem) -> None:
        """Transfer prepared publication to replay before returning execution capacity."""
        prepared = replay.prepare_terminal_replay(job, item)
        if prepared is None:
            self._release_terminal_job(job)
            return
        # The queue marker survives a restart. This in-memory handoff also fences
        # same-directory admission before the next periodic reconciliation.
        self.replay_state.pending_replays[prepared.key] = prepared
        self._release_terminal_job(job)
        try:
            replay.finish_terminal_replay(self.cfg, prepared)
        except Exception:
            logger.exception(
                "Terminal publication remains pending after releasing execution capacity: %s",
                prepared.queue_id,
            )
        else:
            self.replay_state.pending_replays.pop(prepared.key, None)

    def _finalize_completed_job(self, queue_id: str, job: OrcaRunningJob, rc: int) -> None:
        # A child can exit while its engine process is still recorded as active.  Do
        # not publish a terminal queue state (or make the capacity reusable) until
        # that identity has been recovered.  Raising here deliberately leaves the
        # completed job in ``_running`` so the worker retries the whole finalization.
        job.terminal_finalize_pending = True
        recover_slot_engine_process(self.admission_root, job.admission_token)
        pending_item = job.pending_terminal_replay
        if pending_item is not None:
            self._finish_terminal_job(job, pending_item)
            return

        mark_result = replay.mark_terminal_queue_entry(queue_id, job, rc=rc)
        # A no-op is benign only when another actor already moved or removed the
        # queue row. Re-read the pre-mark snapshot before giving up ownership.
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
            self._finish_terminal_job(job, item)
            return
        if mark_result.marked:
            logger.info(
                "Terminal queue generation was already closed before finalizer replay: %s",
                queue_id,
            )
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
        been transferred to durable replay and its slot released; otherwise retry.
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
        try:
            self._finish_terminal_job(job, replay_item)
            return True
        except Exception:
            logger.exception(
                "Failed to prepare or release cancelled job %s; retaining retry ownership",
                queue_id,
            )
            return False

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
