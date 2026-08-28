from __future__ import annotations

import logging
import time
from collections.abc import Callable, MutableMapping
from typing import Any, TypeVar

from ..processes import install_shutdown_signal_handlers
from .models import SlotFillResult

LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


def fill_worker_slots(
    *,
    running_count: Callable[[], int],
    max_concurrent: int,
    reserve_next: Callable[[], tuple[str, T | None]],
    start_reserved: Callable[[T], bool | None],
    max_new_jobs: int | None = None,
) -> SlotFillResult:
    started = 0
    while running_count() < max_concurrent:
        if max_new_jobs is not None and started >= max_new_jobs:
            break
        status, reserved = reserve_next()
        if status != "processed" or reserved is None:
            return SlotFillResult(status="processed" if started else status, started=started)
        before_start = running_count()
        start_result = start_reserved(reserved)
        after_start = running_count()
        if start_result is False or (start_result is None and after_start <= before_start):
            return SlotFillResult(status="processed", started=started)
        started += 1
    return SlotFillResult(status="processed" if started else "idle", started=started)


def pop_completed_worker_jobs(
    running: MutableMapping[str, T],
    *,
    poll_job: Callable[[T], int | None],
    finalize_finished: Callable[[str, T, int], None],
    on_finalize_error: Callable[[str, T, int, Exception], bool | None] | None = None,
    retained_completed_ids: set[str] | None = None,
) -> int:
    completed: list[tuple[str, T, int]] = []
    for queue_id, job in list(running.items()):
        rc = poll_job(job)
        if rc is None:
            continue
        completed.append((queue_id, job, rc))

    for queue_id, job, rc in completed:
        remove_running_job = False
        try:
            finalize_finished(queue_id, job, rc)
        except Exception as exc:
            # Without a handler the caller keeps the default re-raise behavior.
            # With one (the supervised loop), a single job's finalize failure is
            # isolated so the long-running worker survives. The handler must opt
            # in to dropping the job after it has made queue state safe.
            # KeyboardInterrupt and SystemExit are BaseException and intentionally
            # still propagate.
            if on_finalize_error is None:
                raise
            remove_running_job = bool(on_finalize_error(queue_id, job, rc, exc))
        else:
            remove_running_job = True

        if remove_running_job:
            running.pop(queue_id, None)
        elif retained_completed_ids is not None:
            retained_completed_ids.add(queue_id)
    return len(completed)


class QueueWorkerLoop:
    def __init__(
        self,
        *,
        max_concurrent: int,
        poll_interval_seconds: float,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self.poll_interval_seconds = float(poll_interval_seconds)
        self._sleep_fn = sleep_fn or time.sleep
        self._running: dict[str, Any] = {}
        self._shutdown_requested = False

    def run(self) -> int:
        self._install_signal_handlers()
        self._before_run()
        try:
            while not self._shutdown_requested:
                self._run_iteration()
        except KeyboardInterrupt:
            self._shutdown_requested = True
        finally:
            self._shutdown_all()
            self._after_run()
        return 0

    def run_once(
        self,
        *,
        idle_message: str | None = None,
        blocked_message: str | None = None,
    ) -> int:
        self._install_signal_handlers()
        self._before_run()
        try:
            outcome = self._fill_slots(max_new_jobs=1)
            if outcome == "idle":
                if idle_message:
                    print(idle_message)
                return 0
            if outcome == "blocked":
                if blocked_message:
                    print(blocked_message)
                return 0

            while self._running and not self._shutdown_requested:
                self._check_completed_jobs()
                self._check_cancel_requests()
                if self._running:
                    self._sleep()
        except KeyboardInterrupt:
            self._shutdown_requested = True
        finally:
            self._shutdown_all()
            self._after_run()
        return 0

    def _before_run(self) -> None:
        return None

    def _after_run(self) -> None:
        return None

    def _run_iteration(self) -> None:
        self._check_completed_jobs()
        if self._shutdown_requested:
            return
        self._check_cancel_requests()
        if self._shutdown_requested:
            return
        self._fill_slots()
        if self._shutdown_requested:
            return
        self._sleep()

    def _sleep(self) -> None:
        self._sleep_fn(self.poll_interval_seconds)

    def _fill_slots(self, *, max_new_jobs: int | None = None) -> str:
        result = fill_worker_slots(
            running_count=lambda: len(self._running),
            max_concurrent=self.max_concurrent,
            reserve_next=self._reserve_next_entry,
            start_reserved=self._start_reserved,
            max_new_jobs=max_new_jobs,
        )
        return result.status

    def _check_completed_jobs(self, *, retained_completed_ids: set[str] | None = None) -> None:
        pop_completed_worker_jobs(
            self._running,
            poll_job=self._poll_job,
            finalize_finished=self._finalize_completed_job,
            on_finalize_error=self._on_finalize_error,
            retained_completed_ids=retained_completed_ids,
        )

    def _on_finalize_error(self, queue_id: str, job: Any, rc: int, exc: Exception) -> bool:
        del job
        LOGGER.error(
            "worker job finalize failed; keeping job for retry: queue_id=%s rc=%s",
            queue_id,
            rc,
            exc_info=exc,
        )
        return False

    def _running_jobs(self) -> list[tuple[str, Any]]:
        return list(self._running.items())

    def _discard_running_job(self, queue_id: str) -> None:
        self._running.pop(queue_id, None)

    def _check_cancel_requests(self) -> None:
        return None

    def _install_signal_handlers(self) -> None:
        def request_shutdown() -> None:
            self._shutdown_requested = True

        install_shutdown_signal_handlers(request_shutdown)

    def _reserve_next_entry(self) -> tuple[str, Any | None]:
        raise NotImplementedError

    def _start_reserved(self, reserved: Any) -> bool | None:
        raise NotImplementedError

    def _poll_job(self, job: Any) -> int | None:
        raise NotImplementedError

    def _finalize_completed_job(self, queue_id: str, job: Any, rc: int) -> None:
        raise NotImplementedError

    def _shutdown_all(self) -> None:
        raise NotImplementedError


__all__ = [
    "QueueWorkerLoop",
    "fill_worker_slots",
    "pop_completed_worker_jobs",
]
