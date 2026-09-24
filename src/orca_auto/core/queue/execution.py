from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

from ..statuses import STATUS_CANCELLED
from .processes import managed_process_group_has_exited

ProcessResultT = TypeVar("ProcessResultT")


def wait_for_cancellable_process(
    running: Any,
    *,
    finalize_fn: Callable[..., ProcessResultT],
    terminate_process_fn: Callable[[Any], Any],
    should_cancel: Callable[[], bool] | None = None,
    shutdown_requested: Callable[[], bool] | None = None,
    on_shutdown: Callable[[Any], ProcessResultT] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_interval_seconds: float = 1.0,
    check_cancel_before_poll: bool = False,
) -> ProcessResultT:
    process = running.process

    def finish_cancelled() -> ProcessResultT:
        terminate_process_fn(process)
        return finalize_fn(
            running,
            forced_status=STATUS_CANCELLED,
            forced_reason="cancel_requested",
        )

    while True:
        if check_cancel_before_poll and should_cancel is not None and should_cancel():
            return finish_cancelled()

        if process.poll() is not None:
            if not managed_process_group_has_exited(process):
                terminate_process_fn(process)
            return finalize_fn(running)

        if not check_cancel_before_poll and should_cancel is not None and should_cancel():
            return finish_cancelled()

        if shutdown_requested is not None and shutdown_requested():
            terminate_process_fn(process)
            if on_shutdown is not None:
                return on_shutdown(running)
            return finalize_fn(
                running,
                forced_status=STATUS_CANCELLED,
                forced_reason="worker_shutdown",
            )

        sleep_fn(poll_interval_seconds)
