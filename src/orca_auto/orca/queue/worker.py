"""ORCA queue-worker composition under external supervision.

``OrcaQueueWorker`` is the one queue worker: it owns the PID-file lifecycle,
admission (slot reserved before the row is claimed), child start and attach,
terminal finalization, cancellation, shutdown and the recovery pass. The poll
loop it inherits from ``core.queue.worker.loop`` only orders the passes (reap,
cancel, admit, sleep); this worker runs its periodic upkeep before each sleep.
A generation that turned terminal under this worker is settled through the
steps in ``queue/settlement.py`` with the slot release in between; the replay
pipeline in ``queue/replay.py`` is called with explicit state as the last step
of the recovery pass and settles through the same steps.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from orca_auto.core.admission import (
    SlotOwnership,
    admission_dir,
    list_all_slots,
    list_slots,
    live_queue_slot_keys_for_slots,
    reconcile_stale_slots,
    recover_orphaned_engine_slots,
    recover_slot_engine_process,
    release_slot,
    reserve_slot,
    runs_root_ownership,
    update_slot_metadata,
)
from orca_auto.core.admission.records import (
    ENGINE_PROCESS_IDLE,
    SLOT_STATE_ACTIVE,
    SLOT_STATE_RESERVED,
)
from orca_auto.core.queue.deferral import queue_entry_admission_deferral_reason
from orca_auto.core.queue.processes import (
    ManagedProcess,
    start_background_process,
    terminate_process_group,
)
from orca_auto.core.queue.snapshot_intent import reconcile_orphaned_snapshot_generations
from orca_auto.core.queue.store import QueueLockTimeoutError
from orca_auto.core.queue.types import QueueEntry, entry_status_is_running
from orca_auto.core.queue.worker.admission import admission_has_capacity
from orca_auto.core.queue.worker.loop import QueueWorkerLoop
from orca_auto.core.queue.worker.models import ReservedQueueEntry, ReserveStatus
from orca_auto.core.queue.worker.pid_file import (
    WORKER_PID_FILE_NAME,
    remove_worker_pid_file,
    worker_pid_file_path,
    write_worker_pid_file,
)
from orca_auto.core.statuses import STATUS_PENDING, STATUS_RUNNING, TERMINAL_STATUSES
from orca_auto.core.utils.lock import file_lock
from orca_auto.orca.execution_binding import retire_snapshot_intent_for_row
from orca_auto.orca.worker_execution import build_worker_child_command

from ..app_ids import ORCA_ADMISSION_SOURCE, ORCA_AUTO_ORCA_APP_NAME, ORCA_ENGINE_LAUNCH_GATED
from ..config import AppConfig
from ..state_reading import load_state, payload_matches_expected_job_id
from . import publication_repair, replay, roots, settlement
from .adapter import (
    cancel_requested_ids,
    get_cancel_requested,
    get_entry_by_id,
    mark_cancelled,
    mark_failed,
    requeue_running_entry,
    worker_log_path,
)
from .entries import queue_entry_reaction_dir
from .job_records import upsert_row_job_record
from .models import OrcaRunningJob, OrcaWorkerReplayState, TerminalReplayWorkItem
from .notifications import notify_queued_jobs
from .orphans import reconcile_orphaned_running_entries

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
_SNAPSHOT_INTENT_RECONCILE_INTERVAL_SECONDS = 300.0
_WORKER_STATE_RECONCILE_INTERVAL_SECONDS = 60.0


def _try_reserve_admission_slot(
    admission_root: Path, limit: int, *, owned: SlotOwnership
) -> str | None:
    admission_token = reserve_slot(
        admission_root,
        limit,
        source=ORCA_ADMISSION_SOURCE,
        app_name=ORCA_AUTO_ORCA_APP_NAME,
        state=SLOT_STATE_RESERVED,
        engine_process_state=ENGINE_PROCESS_IDLE,
        engine_launch_gated=ORCA_ENGINE_LAUNCH_GATED,
        owned=owned,
    )
    if admission_token is None:
        logger.debug(
            "Queue worker admission paused: admission slots are full (admission_limit=%d)",
            limit,
        )
    return admission_token


def _child_run_concluded(job: OrcaRunningJob) -> bool:
    """True when the child's row is no longer running or its run state is terminal.

    A child that exits non-negatively with a still-running row and a
    non-terminal run state (for example one whose self-requeue write raised
    while it handled the stop) did not conclude; it keeps the resume path.
    """
    current = get_entry_by_id(job.queue_root, job.queue_id)
    if current is None or current.status.value != STATUS_RUNNING:
        return True
    reaction_dir = job.reaction_dir.strip()
    if not reaction_dir:
        return False
    state = load_state(Path(reaction_dir).expanduser().resolve())
    expected_job_id = (job.task_id or "").strip() or current.task_id or None
    if not state or not payload_matches_expected_job_id(state, expected_job_id):
        return False
    return str(state.get("status") or "").strip().lower() in TERMINAL_STATUSES


def _host_core_count() -> int | None:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count()


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
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        # One number is both the concurrency and the admission limit.
        super().__init__(
            max_concurrent=cfg.runtime.max_concurrent,
            poll_interval_seconds=POLL_INTERVAL_SECONDS,
            sleep_fn=sleep_fn,
        )
        self.cfg = cfg
        self.config_path = str(config_path or "").strip()
        self.queue_root = roots.queue_root(cfg)
        self.admission_root = admission_dir(cfg.runtime.allowed_root)
        # Dead-owner slots this worker may recover or drop: those whose work
        # directory lies inside its own runs root. Other records in a store it
        # shares are left as written and still count toward the limit.
        self._owns_slot = runs_root_ownership(cfg.runtime.allowed_root)
        self.replay_state = OrcaWorkerReplayState()
        self._worker_state_last_reconcile: float | None = None
        self._snapshot_intent_last_reconcile: float | None = None
        # Both kept current by ``_admit_next`` before every claim; ``_skip_entry``
        # reads them inside that claim.
        self._admission_withheld_keys: frozenset[str] = frozenset()
        self._publication_withheld_ids: frozenset[str] = frozenset()

    # -- pid file and singleton lock ----------------------------------------

    def _pid_file_path(self) -> Path:
        return worker_pid_file_path(self.queue_root, self.worker_pid_file_name)

    def _lock_file_path(self) -> Path:
        return self._pid_file_path().with_name(f"{self.worker_pid_file_name}.lock")

    def _write_pid_file(self) -> None:
        write_worker_pid_file(self.queue_root, self.worker_pid_file_name)

    def _remove_pid_file(self) -> None:
        remove_worker_pid_file(self.queue_root, self.worker_pid_file_name)

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
            "Queue worker started (pid=%d, max_concurrent=%d, admission_root=%s)",
            os.getpid(),
            self.max_concurrent,
            self.admission_root,
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

    def run_pass(self) -> None:
        try:
            super().run_pass()
        except KeyboardInterrupt:
            logger.info("Queue worker interrupted")
            raise

    def _sleep(self) -> None:
        self._periodic_upkeep()
        super()._sleep()

    def _periodic_upkeep(self) -> None:
        """Queued notification, then the recovery pass when due; runs before each poll sleep."""
        # Deliver accepted submissions even while every execution slot is occupied.
        notify_queued_jobs(self.cfg)
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
        """One recovery pass after a lost parent or child, in this order.

        Snapshot intents (when due), slots reserved but never attached, engine
        records of dead slot owners, then the queue as it stands before row
        reconciliation, stale slots (after collecting the queue ids that live
        slots still hold), orphaned RUNNING rows, and last the terminal replay
        of every transition observed since that queue read or an earlier pass,
        so a row terminalized here or by a child whose parent died still gets
        its location record and notification.
        """
        self._reconcile_snapshot_intents_if_due()
        self._release_unattached_admission_slots()
        recover_orphaned_engine_slots(self.admission_root, strict=False, owned=self._owns_slot)
        before_rows = roots.list_orca_rows(self.cfg)
        protected_queue_keys, protected_queue_ids = live_queue_slot_keys_for_slots(
            list_slots(self.admission_root, owned=self._owns_slot)
        )
        reconcile_stale_slots(self.admission_root, owned=self._owns_slot)
        reconcile_orphaned_running_entries(
            self.queue_root,
            ignore_worker_pid=True,
            protected_queue_keys=protected_queue_keys,
            protected_queue_ids=protected_queue_ids,
        )
        replay.reconcile_terminal_replays(self.cfg, self.replay_state, before_rows)

    def _release_unattached_admission_slots(self) -> None:
        """Release slots this worker reserved but never attached to a job.

        A pass that fails after reserving a slot releases it, and that release
        can fail on the same held admission lock. The slot's owner is this live
        process, so the dead-owner reconcile keeps it and the admission store
        loses that capacity until the worker exits. Attach gives a slot its queue
        id and hands it to the child, and this runs between passes, when no
        reservation of this process is in flight.
        """
        worker_pid = os.getpid()
        for slot in list_all_slots(self.admission_root):
            if slot.owner_pid != worker_pid or slot.state != SLOT_STATE_RESERVED or slot.queue_id:
                continue
            if self._release_admission_slot(slot.token):
                logger.warning(
                    "Released admission slot %s that this worker reserved but never "
                    "attached to a job",
                    slot.token,
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
            removed = reconcile_orphaned_snapshot_generations(self.queue_root)
        except Exception:
            logger.exception("Snapshot orphan reconciliation failed; retaining all candidates")
        else:
            if removed:
                logger.info("Removed %d abandoned pre-enqueue snapshot intent(s)", removed)

    # -- admission ----------------------------------------------------------

    def _release_admission_slot(self, admission_token: str) -> object:
        released: object = release_slot(self.admission_root, admission_token)
        return released

    def _admit_next(self) -> tuple[ReserveStatus, ReservedQueueEntry | None]:
        """Admit one row: the whole reserve-before-claim sequence, in order.

        Withheld directories, publication repair, queued notification, capacity,
        preview, slot reservation, claim by id, and slot release when the claim
        is lost.
        """
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
        if withheld != self._admission_withheld_keys:
            if withheld:
                logger.warning(
                    "ORCA admission withheld for reaction dir(s) until terminal replay completes: %s",
                    ", ".join(sorted(withheld)),
                )
            else:
                logger.info("ORCA admission is no longer withheld for any reaction dir")
            self._admission_withheld_keys = withheld
        publication_withheld_ids = publication_repair.repair_queue_publications(self.cfg)
        if publication_withheld_ids is None:
            logger.warning("Queue admission paused: ORCA queue could not be inspected")
            return "blocked", None
        self._publication_withheld_ids = publication_withheld_ids
        # Load-bearing, not a duplicate of the poll-sleep call: a row the repair
        # above just published and the claim below takes in this same pass is
        # never pending at a later sleep, so it would never be notified.
        notify_queued_jobs(self.cfg)
        # Read before writing, and read the cheap thing first: a full pool is one
        # lock-free admission read, and an empty queue is a queue listing, while
        # an admission reservation is a durable write to the shared slot file that
        # an idle worker must not pay (twice, with the release) on every poll. The
        # slot still comes before the dequeue so a claimed row always holds
        # capacity; a preview that loses the race simply releases the slot again.
        if not admission_has_capacity(self.admission_root, self.max_concurrent):
            return "blocked", None
        if roots.peek_next_entry(self.cfg, skip_entry_fn=self._skip_entry) is None:
            return "idle", None
        admission_token = _try_reserve_admission_slot(
            self.admission_root, self.max_concurrent, owned=self._owns_slot
        )
        if admission_token is None:
            return "blocked", None
        try:
            dequeued = roots.dequeue_next_entry(self.cfg, skip_entry_fn=self._skip_entry)
        except Exception:
            self._release_admission_slot(admission_token)
            raise
        if dequeued is None:
            self._release_admission_slot(admission_token)
            return "idle", None
        queue_root, entry = dequeued
        return "processed", ReservedQueueEntry(
            queue_root=queue_root, entry=entry, admission_token=admission_token
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
                reaction_dirs.append(job.reaction_dir)
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
                key = settlement.reaction_key_for_dir(reaction_dir)
            except (OSError, RuntimeError):
                return None
            if not key:
                return None
            keys.add(key)
        return frozenset(keys)

    def _entry_waits_for_terminal_replay(self, entry: QueueEntry) -> bool:
        """Whether claiming *entry* would start a generation in a withheld directory."""
        withheld = self._admission_withheld_keys
        if not withheld:
            return False
        try:
            key = settlement.reaction_dir_key(entry)
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
            entry.queue_id in self._running
            or entry.queue_id in self._publication_withheld_ids
            or self._entry_waits_for_terminal_replay(entry)
        )

    # -- child start --------------------------------------------------------

    def _start_reserved(self, reserved: ReservedQueueEntry) -> bool:
        try:
            # Retire the journal before execution so even a very fast terminal
            # job cannot lose its queue row while an ENQUEUEING intent remains.
            retire_snapshot_intent_for_row(reserved.queue_root, reserved.entry)
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
        log_path = str(worker_log_path(queue_root, entry.queue_id))
        return start_background_process(
            build_worker_child_command(
                config_path=self.config_path,
                queue_root=queue_root,
                queue_id=entry.queue_id,
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

        self._running[entry.queue_id] = self._make_running_job(
            queue_root=queue_root,
            entry=entry,
            process=proc,
            admission_token=admission_token,
        )
        return True

    def _terminate_untracked_process(self, process: ManagedProcess) -> None:
        with contextlib.suppress(Exception):
            process.terminate()

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
            queue_id=entry.queue_id,
            reaction_dir=queue_entry_reaction_dir(entry),
            task_id=entry.task_id or None,
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
        logger.error("Failed to start job %s: %s", entry.queue_id, exc)
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
                entry.queue_id,
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
        queue_id = entry.queue_id
        attached = update_slot_metadata(
            self.admission_root,
            admission_token,
            state=SLOT_STATE_ACTIVE,
            queue_id=queue_id,
            app_name=entry.app_name,
            task_id=entry.task_id,
            owner_pid=process.pid,
            work_dir=queue_entry_reaction_dir(entry) or None,
            owned=self._owns_slot,
        )
        if not attached:
            logger.error(
                "Failed to attach queue identity to admission slot %s for job %s",
                admission_token,
                queue_id,
            )
            return False
        try:
            upsert_row_job_record(self.cfg, entry, STATUS_RUNNING, require_task_id=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to update running job location for %s: %s", queue_id, exc)
        return True

    # -- terminal finalization ---------------------------------------------

    def _release_terminal_job(self, job: OrcaRunningJob) -> None:
        """Release capacity before clearing the state needed to retry a failed release."""
        self._release_admission_slot(job.admission_token)
        job.pending_terminal_replay = None
        job.terminal_finalize_pending = False

    def _settle_live(self, job: OrcaRunningJob, item: TerminalReplayWorkItem) -> None:
        """Settle a generation this worker supervised: prepare, bind, release, then finish.

        The item stays on the job until replay bookkeeping holds it and the slot
        is released, so a failure up to the release retries the settlement with
        the slot kept and the directory withheld from admission.
        """
        job.pending_terminal_replay = item
        if settlement.is_superseded(item):
            settlement.retire_marker(item)
            self._release_terminal_job(job)
            return
        item = settlement.prepare(item)
        job.pending_terminal_replay = item
        item = settlement.bind_row(item)
        job.pending_terminal_replay = item
        # The queue marker survives a restart. This in-memory handoff also fences
        # same-directory admission before the next periodic reconciliation.
        self.replay_state.pending_replays[item.queue_id] = item
        self._release_terminal_job(job)
        try:
            settlement.finish(self.cfg, item)
        except Exception:
            logger.exception(
                "Terminal publication remains pending after releasing execution capacity: %s",
                item.queue_id,
            )
        else:
            self.replay_state.pending_replays.pop(item.queue_id, None)

    def _hand_off_terminal_row(self, job: OrcaRunningJob, entry: QueueEntry | None) -> bool:
        """Settle the generation a marked terminal row still owes, else only release the job.

        Returns whether there was a generation to settle.
        """
        item = settlement.work_item_for_row(job.queue_root, entry)
        if item is None:
            self._release_terminal_job(job)
            return False
        self._settle_live(job, item)
        return True

    def _finalize_completed_job(self, queue_id: str, job: OrcaRunningJob, rc: int) -> None:
        # A child can exit while its engine process is still recorded as active.  Do
        # not publish a terminal queue state (or make the capacity reusable) until
        # that identity has been recovered.  Raising here deliberately leaves the
        # completed job in ``_running`` so the worker retries the whole finalization.
        job.terminal_finalize_pending = True
        recover_slot_engine_process(self.admission_root, job.admission_token)
        pending_item = job.pending_terminal_replay
        if pending_item is not None:
            self._settle_live(job, pending_item)
            return

        marked = settlement.mark_terminal_row(
            job.queue_root, queue_id, task_id=job.task_id, reaction_dir=job.reaction_dir, rc=rc
        )
        # A no-op is benign only when another actor already moved or removed the
        # queue row. Re-read the pre-mark snapshot before giving up ownership.
        current_after_mark = get_entry_by_id(job.queue_root, queue_id)
        if entry_status_is_running(current_after_mark):
            raise RuntimeError(
                "terminal queue mark did not update the running entry; "
                f"retaining retry ownership for {queue_id}"
            )
        deferral_reason = (
            queue_entry_admission_deferral_reason(current_after_mark)
            if current_after_mark is not None and current_after_mark.status.value == STATUS_PENDING
            else ""
        )
        if deferral_reason:
            logger.warning(
                "ORCA job %s was not started and waits in the queue: %s",
                queue_id,
                deferral_reason,
            )
        if not self._hand_off_terminal_row(job, current_after_mark) and marked:
            logger.info(
                "Terminal queue generation was already closed before finalizer replay: %s",
                queue_id,
            )

    # -- cancellation -------------------------------------------------------

    def _check_cancel_requests(self) -> None:
        # Reaped children retained for completion retry must never be signalled.
        jobs = [
            (queue_id, job) for queue_id, job in self._running_jobs() if job.process.poll() is None
        ]
        if not jobs:
            return
        try:
            requested = cancel_requested_ids(
                self.queue_root, {queue_id: job.task_id or None for queue_id, job in jobs}
            )
        except QueueLockTimeoutError:
            return  # Retry next pass.
        for queue_id, job in jobs:
            if queue_id in requested and job.process.poll() is None:
                if self._cancel_running_job(queue_id, job) is True:
                    self._discard_running_job(queue_id)

    def _stop_child_and_recover_engine(
        self, job: OrcaRunningJob, *, mark_finalize_pending: bool
    ) -> bool:
        """Stop the child's process group and, once it exited, recover its engine record.

        Returns whether the group stopped. Cancellation marks the job
        finalize-pending before the recovery; shutdown leaves that to the
        finalization or the requeue that follows.
        """
        terminated = terminate_process_group(job.process)
        if terminated is True and job.process.poll() is not None:
            if mark_finalize_pending:
                job.terminal_finalize_pending = True
            recover_slot_engine_process(self.admission_root, job.admission_token)
        return terminated is True

    def _cancel_running_job(self, queue_id: str, job: OrcaRunningJob) -> bool:
        """Stop *job*, mark its row cancelled unless its child did, then settle the generation.

        Returns ``True`` only when the job's queue and admission ownership has
        been transferred to durable replay and its slot released; otherwise retry.
        """
        logger.info("Cancelling running job: %s", queue_id)
        try:
            terminated = self._stop_child_and_recover_engine(job, mark_finalize_pending=True)
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
        if not terminated or not process_exited:
            logger.error(
                "Running job %s did not fully stop; retaining queue entry and admission slot %s",
                queue_id,
                job.admission_token,
            )
            return False
        try:
            current = get_entry_by_id(self.queue_root, queue_id)
            if current is None:
                return False
            marked = mark_cancelled(
                self.queue_root,
                queue_id,
                expected_entry=current,
                expected_task_id=job.task_id or None,
            )
        except Exception:
            logger.exception(
                "Failed to durably mark cancelled ORCA job %s; retaining retry ownership",
                queue_id,
            )
            return False
        terminal_entry = get_entry_by_id(self.queue_root, queue_id)
        # mark_cancelled accepts only a running row. A child that handled the
        # stop with the cancel pending already marked its own row cancelled with
        # the replay marker (requeue_running_entry), so a refusal of a row that
        # has left running settles what the row owes, as the completion path
        # does. Any other refusal keeps the job for retry.
        if not marked and (terminal_entry is None or entry_status_is_running(terminal_entry)):
            return False
        if entry_status_is_running(terminal_entry):
            logger.error(
                "Cancellation returned without a durable terminal queue transition: %s",
                queue_id,
            )
            return False
        # A row without a marker was already settled by another owner while the
        # stale cancellation snapshot was in flight: the job is only released.
        try:
            self._hand_off_terminal_row(job, terminal_entry)
        except Exception:
            logger.exception(
                "Failed to settle or release cancelled job %s; retaining retry ownership",
                queue_id,
            )
            return False
        return True

    # -- shutdown -----------------------------------------------------------

    def _before_shutdown_all(self, running_count: int) -> None:
        logger.info("Shutting down %d running job(s)...", running_count)

    def _shutdown_running_job(self, queue_id: str, job: OrcaRunningJob) -> None:
        try:
            cancel_requested = get_cancel_requested(
                self.queue_root,
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
            # row is marked cancelled with its replay marker by the child, which handles
            # the stop through requeue_running_entry, or by mark_cancelled when the
            # child was killed first. The cancel path then settles it before the worker
            # exits (the cancelled run state, the location record and the notification)
            # instead of leaving a "running" run state to the next start's replay.
            self._cancel_running_job(queue_id, job)
            return

        if not self._stop_child_and_recover_engine(job, mark_finalize_pending=False):
            logger.error(
                "Process for running job %s did not stop; leaving queue entry running "
                "and retaining admission slot %s",
                queue_id,
                job.admission_token,
            )
            return
        exit_code = job.process.poll()
        if exit_code is not None and exit_code >= 0 and _child_run_concluded(job):
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
            current = get_entry_by_id(self.queue_root, queue_id)
            if current is not None:
                requeue_running_entry(
                    self.queue_root,
                    queue_id,
                    expected_entry=current,
                    expected_task_id=job.task_id or None,
                )
        finally:
            self._release_admission_slot(job.admission_token)
