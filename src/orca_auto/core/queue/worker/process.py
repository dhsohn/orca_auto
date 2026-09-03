from __future__ import annotations

import contextlib
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from orca_auto.core.queue.engine.snapshot_intent import (
    finalize_queued_snapshot_intent,
    reconcile_orphaned_snapshot_generations,
    snapshot_runtime_roots_for_cfg,
)
from orca_auto.core.utils.lock import file_lock

from ..processes import (
    remove_worker_pid_file,
    terminate_process_group,
    worker_pid_file_path,
    write_worker_pid_file,
)
from .loop import QueueWorkerLoop
from .models import BackgroundRunningJob

logger = logging.getLogger(__name__)
_SNAPSHOT_INTENT_RECONCILE_INTERVAL_SECONDS = 300.0
_WORKER_STATE_RECONCILE_INTERVAL_SECONDS = 60.0


class _PollableProcess(Protocol):
    def poll(self) -> int | None: ...


class _ProcessBackedJob(Protocol):
    process: _PollableProcess


@dataclass(frozen=True)
class PidFileChildProcessQueueWorkerHooks:
    handle_worker_start_error: Callable[[Any, Path, Any, str, OSError], None]
    on_worker_process_started: Callable[[Any, Path, Any, Any, str], bool]
    finalize_completed_job: Callable[[Any, str, Any, int], None]
    shutdown_running_job: Callable[[Any, str, Any], None]
    reconcile_worker_state: Callable[[Any], None]
    before_shutdown_all: Callable[[Any, int], None] | None = None


class QueueWorkerPidFileMixin:
    worker_pid_file_name = "queue_worker.pid"
    worker_lock_timeout_seconds = 0.0
    allowed_root: Path

    def _pid_file_path(self) -> Path:
        return worker_pid_file_path(self.allowed_root, self.worker_pid_file_name)

    def _lock_file_path(self) -> Path:
        return self._pid_file_path().with_name(f"{self.worker_pid_file_name}.lock")

    def _write_pid_file(self) -> None:
        write_worker_pid_file(self.allowed_root, self.worker_pid_file_name)

    def _remove_pid_file(self) -> None:
        remove_worker_pid_file(self.allowed_root, self.worker_pid_file_name)


class ChildProcessQueueWorker(QueueWorkerLoop):
    def __init__(
        self,
        cfg: Any,
        *,
        config_path: str,
        max_concurrent: int | None = None,
        deps: Any,
    ) -> None:
        configured_max = cfg.runtime.max_concurrent if max_concurrent is None else max_concurrent
        super().__init__(
            max_concurrent=max(1, int(configured_max)),
            poll_interval_seconds=deps.poll_interval_seconds,
            sleep_fn=lambda seconds: deps.time.sleep(seconds),
        )
        self.cfg = cfg
        self.config_path = config_path
        self.admission_root: str | Path = deps.admission_root(cfg)
        self.deps: Any = deps

    def _before_run(self) -> None:
        self._reconcile_worker_state_now()

    def _reconcile_worker_state_now(self) -> None:
        self._reconcile_worker_state()
        self.__dict__["_worker_state_last_reconcile_monotonic"] = time.monotonic()

    def _reconcile_worker_state_if_due(self) -> None:
        now = time.monotonic()
        last_reconcile = self.__dict__.get("_worker_state_last_reconcile_monotonic")
        if (
            last_reconcile is None
            or now - float(last_reconcile) >= _WORKER_STATE_RECONCILE_INTERVAL_SECONDS
        ):
            self._reconcile_worker_state_now()

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

    def run_once(
        self,
        *,
        idle_message: str | None = "No pending jobs.",
        blocked_message: str | None = "status: waiting_for_slot",
    ) -> int:
        return super().run_once(
            idle_message=idle_message,
            blocked_message=blocked_message,
        )

    def _reserve_next_entry(self) -> tuple[str, Any | None]:
        deps = self.deps
        reserved: tuple[str, Any | None] = deps.reserve_dequeued_entry(
            self.cfg,
            admission_root=self.admission_root,
            reserve_slot_fn=deps.try_reserve_admission_slot,
            dequeue_next_fn=deps.dequeue_next_entry,
            release_slot_fn=deps.release_slot,
        )
        return reserved

    def _start_reserved(self, reserved: Any) -> bool:
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

    def _start_job(self, queue_root: Path, entry: Any, *, admission_token: str) -> bool:
        deps = self.deps
        try:
            proc = deps.start_background_job_process(
                config_path=self.config_path,
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
                self._terminate_untracked_process(proc)
                self._handle_worker_start_error(
                    queue_root,
                    entry,
                    admission_token,
                    OSError("worker attach rejected"),
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

        self._running[self._running_queue_id(entry)] = self._make_running_job(
            queue_root=queue_root,
            entry=entry,
            process=proc,
            admission_token=admission_token,
        )
        return True

    def _terminate_untracked_process(self, process: Any) -> None:
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            with contextlib.suppress(Exception):
                terminate()

    def _handle_worker_start_error(
        self,
        queue_root: Path,
        entry: Any,
        admission_token: str,
        exc: OSError,
    ) -> None:
        raise NotImplementedError

    def _on_worker_process_started(
        self,
        queue_root: Path,
        entry: Any,
        *,
        process: Any,
        admission_token: str,
    ) -> bool:
        del queue_root, entry, process, admission_token
        return True

    def _running_queue_id(self, entry: Any) -> str:
        return str(entry.queue_id)

    def _make_running_job(
        self,
        *,
        queue_root: Path,
        entry: Any,
        process: Any,
        admission_token: str,
    ) -> Any:
        return BackgroundRunningJob(
            queue_root=queue_root,
            entry=entry,
            process=process,
            admission_token=admission_token,
        )

    def _poll_job(self, job: _ProcessBackedJob) -> int | None:
        return job.process.poll()

    def _release_admission_slot(self, admission_token: str) -> object:
        released: object = self.deps.release_slot(self.admission_root, admission_token)
        return released

    def _mark_entry_failed_and_release(
        self,
        queue_root: Path,
        entry: Any,
        admission_token: str,
        *,
        error: str,
        mark_failed_fn: Callable[..., Any],
    ) -> None:
        try:
            mark_failed_fn(
                queue_root,
                self._running_queue_id(entry),
                error=error,
                expected_entry=entry,
            )
        finally:
            self._release_admission_slot(admission_token)

    def _finalize_completed_job(self, _queue_id: str, job: Any, rc: int) -> None:
        raise NotImplementedError

    def _shutdown_all(self) -> None:
        if not self._running:
            return
        # Reap any child that already finished (e.g. it completed during the final
        # poll interval, before the shutdown-triggered loop exit) through the normal
        # completion path first. Otherwise such a job -- still tracked as running --
        # would be force-terminated and requeued below, and needlessly re-executed
        # from scratch on the next worker start.
        retained_completed_ids: set[str] = set()
        self._check_completed_jobs(retained_completed_ids=retained_completed_ids)
        jobs_to_shutdown = [
            (queue_id, job)
            for queue_id, job in self._running_jobs()
            if queue_id not in retained_completed_ids
        ]
        if not jobs_to_shutdown:
            return
        self._before_shutdown_all(len(jobs_to_shutdown))
        for queue_id, job in jobs_to_shutdown:
            try:
                self._shutdown_running_job(queue_id, job)
            except Exception:
                # One job's shutdown failing must not leave the remaining
                # children running unsupervised, nor this one: the child runs
                # in its own session and receives no signal aimed at the
                # worker, so stop it here as a last resort. Its row and slot
                # are reconciled on the next worker start.
                logger.exception("Shutting down running job %s failed", queue_id)
                self._stop_child_as_last_resort(queue_id, job)
            self._discard_running_job(queue_id)

    def _stop_child_as_last_resort(self, queue_id: str, job: Any) -> None:
        process = getattr(job, "process", None)
        if process is None:
            return
        try:
            stopped = terminate_process_group(process)
        except Exception:
            logger.exception("Stopping the child of job %s as a last resort failed", queue_id)
            return
        if stopped is not True:
            logger.error(
                "Child of job %s could not be confirmed stopped by the worker's last-resort "
                "stop; inspect its process group before the next worker start, which "
                "reconciles its queue row and admission slot",
                queue_id,
            )

    def _before_shutdown_all(self, running_count: int) -> None:
        del running_count

    def _shutdown_running_job(self, queue_id: str, job: Any) -> None:
        raise NotImplementedError

    def _reconcile_worker_state(self) -> None:
        raise NotImplementedError


class PidFileChildProcessQueueWorker(QueueWorkerPidFileMixin, ChildProcessQueueWorker):
    """Child-process queue worker with standard orca_auto pid-file lifecycle."""

    def __init__(
        self,
        cfg: Any,
        *,
        config_path: str,
        max_concurrent: int | None = None,
        deps: Any,
        allowed_root: str | Path | None = None,
        admission_root: str | Path | None = None,
    ) -> None:
        super().__init__(
            cfg,
            config_path=config_path,
            max_concurrent=max_concurrent,
            deps=deps,
        )
        raw_allowed_root = allowed_root if allowed_root is not None else cfg.runtime.allowed_root
        self.allowed_root = Path(str(raw_allowed_root)).expanduser().resolve()
        if admission_root is not None:
            self.admission_root = Path(str(admission_root)).expanduser().resolve()

    def run(self) -> int:
        lock_path = self._lock_file_path()
        try:
            with file_lock(lock_path, timeout_seconds=self.worker_lock_timeout_seconds):
                return super().run()
        except TimeoutError:
            print(
                f"error: queue worker already running (lock={lock_path})",
                file=sys.stderr,
            )
            return 1

    def run_once(
        self,
        *,
        idle_message: str | None = "No pending jobs.",
        blocked_message: str | None = "status: waiting_for_slot",
    ) -> int:
        lock_path = self._lock_file_path()
        try:
            with file_lock(lock_path, timeout_seconds=self.worker_lock_timeout_seconds):
                return super().run_once(
                    idle_message=idle_message,
                    blocked_message=blocked_message,
                )
        except TimeoutError:
            print(
                f"error: queue worker already running (lock={lock_path})",
                file=sys.stderr,
            )
            return 1

    def _before_run(self) -> None:
        self._write_pid_file()
        super()._before_run()

    def _after_run(self) -> None:
        self._remove_pid_file()


class HookedPidFileChildProcessQueueWorker(PidFileChildProcessQueueWorker):
    """Pid-file queue worker whose engine-specific behavior is supplied as hooks."""

    def __init__(
        self,
        cfg: Any,
        *,
        config_path: str,
        max_concurrent: int | None = None,
        deps: Any,
        hooks: PidFileChildProcessQueueWorkerHooks,
        worker_pid_file_name: str | None = None,
        allowed_root: str | Path | None = None,
        admission_root: str | Path | None = None,
    ) -> None:
        if worker_pid_file_name is not None:
            self.worker_pid_file_name = worker_pid_file_name
        self.hooks = hooks
        super().__init__(
            cfg,
            config_path=config_path,
            max_concurrent=max_concurrent,
            deps=deps,
            allowed_root=allowed_root,
            admission_root=admission_root,
        )

    def _handle_worker_start_error(
        self,
        queue_root: Path,
        entry: Any,
        admission_token: str,
        exc: OSError,
    ) -> None:
        self.hooks.handle_worker_start_error(
            self,
            queue_root,
            entry,
            admission_token,
            exc,
        )

    def _on_worker_process_started(
        self,
        queue_root: Path,
        entry: Any,
        *,
        process: Any,
        admission_token: str,
    ) -> bool:
        return self.hooks.on_worker_process_started(
            self,
            queue_root,
            entry,
            process,
            admission_token,
        )

    def _finalize_completed_job(self, queue_id: str, job: Any, rc: int) -> None:
        self.hooks.finalize_completed_job(self, queue_id, job, rc)

    def _before_shutdown_all(self, running_count: int) -> None:
        if self.hooks.before_shutdown_all is not None:
            self.hooks.before_shutdown_all(self, running_count)

    def _shutdown_running_job(self, queue_id: str, job: Any) -> None:
        self.hooks.shutdown_running_job(self, queue_id, job)

    def _reconcile_worker_state(self) -> None:
        now = time.monotonic()
        last_reconcile = self.__dict__.get("_snapshot_intent_last_reconcile_monotonic")
        if (
            last_reconcile is None
            or now - float(last_reconcile) >= _SNAPSHOT_INTENT_RECONCILE_INTERVAL_SECONDS
        ):
            self.__dict__["_snapshot_intent_last_reconcile_monotonic"] = now
            try:
                removed = reconcile_orphaned_snapshot_generations(
                    snapshot_runtime_roots_for_cfg(self.cfg)
                )
            except Exception:
                logger.exception("Snapshot orphan reconciliation failed; retaining all candidates")
            else:
                if removed:
                    logger.info(
                        "Removed %d abandoned pre-enqueue snapshot intent(s)",
                        removed,
                    )
        self.hooks.reconcile_worker_state(self)


__all__ = [
    "ChildProcessQueueWorker",
    "HookedPidFileChildProcessQueueWorker",
    "PidFileChildProcessQueueWorker",
    "PidFileChildProcessQueueWorkerHooks",
    "QueueWorkerPidFileMixin",
]
