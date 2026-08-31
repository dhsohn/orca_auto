from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, NoReturn, TypeVar

from orca_auto.core.queue import execution as _queue_execution
from orca_auto.core.queue.processes import managed_process_group_has_exited

ProcessResultT = TypeVar("ProcessResultT")


class ProcessCleanupError(RuntimeError):
    """Raised when a live engine process cannot be confirmed terminated."""


def _process_has_exited(process: Any) -> bool:
    return managed_process_group_has_exited(process)


def retain_process_ownership_until_exit(
    process: Any,
    *,
    terminate_process: Callable[[Any], bool],
    sleep: Callable[[float], None] = time.sleep,
    retry_attempts: int = 2,
    poll_interval_seconds: float = 1.0,
) -> None:
    """Do not let the owning worker exit while its engine process is live.

    Termination is retried a bounded number of times.  If it still cannot be
    confirmed, the owner remains alive and only polls for natural/external
    process exit; this keeps the admission slot fail-closed.
    """
    interval = max(0.0, float(poll_interval_seconds))
    for _attempt in range(max(0, int(retry_attempts))):
        if _process_has_exited(process):
            return
        sleep(interval)
        try:
            terminate_process(process)
        except Exception:  # noqa: BLE001
            pass
        if _process_has_exited(process):
            return

    while not _process_has_exited(process):
        sleep(interval)


@dataclass(frozen=True)
class CancellableProcessExecution(Generic[ProcessResultT]):
    start_job: Callable[[], Any]
    finalize_job: Callable[..., ProcessResultT]
    terminate_process: Callable[[Any], bool]
    build_failure_result: Callable[[Exception], ProcessResultT]
    prepare_running_job: Callable[[], None] | None = None
    wait_for_cancellable_process: Callable[..., ProcessResultT] = (
        _queue_execution.wait_for_cancellable_process
    )
    should_cancel: Callable[[], bool] | None = None
    shutdown_requested: Callable[[], bool] | None = None
    on_shutdown: Callable[[Any], ProcessResultT] | None = None
    sleep: Callable[[float], None] | None = None
    poll_interval_seconds: float = 1.0
    check_cancel_before_poll: bool = False
    register_running_job: Callable[[Any | None], None] | None = None
    should_reraise_exception: Callable[[Exception], bool] | None = None
    cleanup_retry_attempts: int = 2
    cleanup_poll_interval_seconds: float | None = None


def run_cancellable_process_execution(
    actions: CancellableProcessExecution[ProcessResultT],
) -> ProcessResultT:
    running: Any | None = None
    running_registered = False
    preparation_completed = False
    safe_start_failure = False
    termination_completed = False

    def attempt_process_termination(process: Any) -> ProcessCleanupError | None:
        nonlocal termination_completed
        try:
            result = actions.terminate_process(process)
        except ProcessCleanupError as exc:
            return exc
        except Exception as exc:  # noqa: BLE001
            cleanup_error = ProcessCleanupError(
                "Process cleanup raised before termination could be confirmed"
            )
            cleanup_error.__cause__ = exc
            return cleanup_error
        if result is not True:
            return ProcessCleanupError("Process cleanup did not confirm successful termination")
        try:
            return_code = process.poll()
        except Exception as exc:  # noqa: BLE001
            cleanup_error = ProcessCleanupError(
                "Process cleanup succeeded but process exit could not be verified"
            )
            cleanup_error.__cause__ = exc
            return cleanup_error
        if return_code is None:
            return ProcessCleanupError(
                "Process cleanup reported success while the process is still running"
            )
        termination_completed = True
        return None

    def terminate_running_process(process: Any) -> bool:
        cleanup_error = attempt_process_termination(process)
        if cleanup_error is not None:
            raise cleanup_error
        return True

    def finalize_after_process_exit(*args: Any, **kwargs: Any) -> ProcessResultT:
        # The waiter invokes its finalizer only after the process group has
        # exited (naturally or through terminate_running_process). Mark that
        # fact before user finalization, because a slow/raising finalizer must
        # not make the exception path signal a newly reused numeric PGID.
        nonlocal termination_completed
        termination_completed = True
        return actions.finalize_job(*args, **kwargs)

    def retain_ownership_until_process_exit(
        process: Any,
        cleanup_error: ProcessCleanupError,
    ) -> NoReturn:
        """Keep the child/admission owner alive until the engine is actually gone."""
        nonlocal termination_completed
        poll_interval = (
            actions.poll_interval_seconds
            if actions.cleanup_poll_interval_seconds is None
            else actions.cleanup_poll_interval_seconds
        )
        retain_process_ownership_until_exit(
            process,
            terminate_process=actions.terminate_process,
            sleep=actions.sleep or time.sleep,
            retry_attempts=actions.cleanup_retry_attempts,
            poll_interval_seconds=poll_interval,
        )
        termination_completed = True
        raise cleanup_error

    try:
        if actions.prepare_running_job is not None:
            actions.prepare_running_job()
            preparation_completed = True
        running = actions.start_job()
        if actions.register_running_job is not None:
            actions.register_running_job(running)
            running_registered = True
        wait_kwargs: dict[str, Any] = {
            "finalize_fn": finalize_after_process_exit,
            "terminate_process_fn": terminate_running_process,
            "should_cancel": actions.should_cancel,
            "shutdown_requested": actions.shutdown_requested,
            "on_shutdown": actions.on_shutdown,
            "poll_interval_seconds": actions.poll_interval_seconds,
            "check_cancel_before_poll": actions.check_cancel_before_poll,
        }
        if actions.sleep is not None:
            wait_kwargs["sleep_fn"] = actions.sleep
        return actions.wait_for_cancellable_process(running, **wait_kwargs)
    except BaseException as exc:
        if running is None and preparation_completed and isinstance(exc, Exception):
            # Internal start_job implementations clean any post-Popen failure
            # before raising an ordinary Exception. Async BaseException with no
            # returned handle remains ambiguous and must keep pending fenced.
            safe_start_failure = True
        if isinstance(exc, ProcessCleanupError):
            if running is not None and not termination_completed:
                retain_ownership_until_process_exit(running.process, exc)
            raise
        if running is not None and not termination_completed:
            try:
                terminate_running_process(running.process)
            except ProcessCleanupError as cleanup_exc:
                try:
                    retain_ownership_until_process_exit(running.process, cleanup_exc)
                except ProcessCleanupError as guarded_cleanup_exc:
                    raise guarded_cleanup_exc from exc
        if not isinstance(exc, Exception):
            raise
        if actions.should_reraise_exception is not None and actions.should_reraise_exception(exc):
            raise
        return actions.build_failure_result(exc)
    finally:
        if actions.register_running_job is not None and (
            running_registered
            or running is not None
            or (preparation_completed and safe_start_failure)
        ):
            try:
                actions.register_running_job(None)
            except BaseException:
                # A durable record must never be cleared while the group is
                # still alive. If the registrar detects that condition, keep
                # this admission owner alive, finish cleanup, and retry the
                # clear before propagating the bookkeeping failure.
                if running is not None and not _process_has_exited(running.process):
                    retain_process_ownership_until_exit(
                        running.process,
                        terminate_process=actions.terminate_process,
                        sleep=actions.sleep or time.sleep,
                        retry_attempts=actions.cleanup_retry_attempts,
                        poll_interval_seconds=(
                            actions.poll_interval_seconds
                            if actions.cleanup_poll_interval_seconds is None
                            else actions.cleanup_poll_interval_seconds
                        ),
                    )
                    actions.register_running_job(None)
                raise


def run_cancellable_engine_process(
    *,
    prepare_running_job: Callable[[], None] | None = None,
    start_job: Callable[[], Any],
    finalize_job: Callable[..., ProcessResultT],
    terminate_process: Callable[[Any], bool],
    build_failure_result: Callable[[Exception], ProcessResultT],
    wait_for_cancellable_process: Callable[..., ProcessResultT] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    shutdown_requested: Callable[[], bool] | None = None,
    on_shutdown: Callable[[Any], ProcessResultT] | None = None,
    sleep: Callable[[float], None] | None = None,
    poll_interval_seconds: float = 1.0,
    check_cancel_before_poll: bool = False,
    register_running_job: Callable[[Any | None], None] | None = None,
    should_reraise_exception: Callable[[Exception], bool] | None = None,
    cleanup_retry_attempts: int = 2,
    cleanup_poll_interval_seconds: float | None = None,
) -> ProcessResultT:
    execution_kwargs: dict[str, Any] = {}
    if wait_for_cancellable_process is not None:
        execution_kwargs["wait_for_cancellable_process"] = wait_for_cancellable_process
    actions: CancellableProcessExecution[ProcessResultT] = CancellableProcessExecution(
        prepare_running_job=prepare_running_job,
        start_job=start_job,
        finalize_job=finalize_job,
        terminate_process=terminate_process,
        build_failure_result=build_failure_result,
        should_cancel=should_cancel,
        shutdown_requested=shutdown_requested,
        on_shutdown=on_shutdown,
        sleep=sleep,
        poll_interval_seconds=poll_interval_seconds,
        check_cancel_before_poll=check_cancel_before_poll,
        register_running_job=register_running_job,
        should_reraise_exception=should_reraise_exception,
        cleanup_retry_attempts=cleanup_retry_attempts,
        cleanup_poll_interval_seconds=cleanup_poll_interval_seconds,
        **execution_kwargs,
    )
    return run_cancellable_process_execution(actions)


__all__ = [
    "CancellableProcessExecution",
    "ProcessCleanupError",
    "retain_process_ownership_until_exit",
    "run_cancellable_engine_process",
    "run_cancellable_process_execution",
]
