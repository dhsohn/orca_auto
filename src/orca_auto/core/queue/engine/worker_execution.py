from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from ..cancellable import run_cancellable_engine_process
from .lifecycle import EngineWorkerLifecycle, run_engine_worker_lifecycle

DependencyT = TypeVar("DependencyT", covariant=True)

CancelRequested = Callable[..., bool]
CancellableProcessWaiter = Callable[..., Any]
DependencyBuilder = Callable[..., DependencyT]
DependencyFactory = Callable[[], Any]
FailureResultBuilder = Callable[[Exception], Any]
NowUtcIso = Callable[[], str]
ProcessTerminator = Callable[[Any], bool]
QueueStatusMarker = Callable[..., Any]
SleepFn = Callable[[float], None]
StartJob = Callable[[], Any]


class WorkerShutdownRequested(RuntimeError):
    def __init__(self, context: Any) -> None:
        super().__init__("worker_shutdown")
        self.context = context


class CancellableJobFinalizer(Protocol):
    def __call__(
        self,
        running: Any,
        *,
        forced_status: str | None = None,
        forced_reason: str | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class EngineWorkerTimingDependencies:
    now_utc_iso: NowUtcIso


@dataclass(frozen=True)
class EngineWorkerQueueDependencies:
    get_cancel_requested: CancelRequested
    mark_completed: QueueStatusMarker
    mark_cancelled: QueueStatusMarker
    mark_failed: QueueStatusMarker


@dataclass(frozen=True)
class EngineWorkerProcessDependencies:
    terminate_process: ProcessTerminator
    wait_for_cancellable_process: CancellableProcessWaiter
    sleep: SleepFn
    cancel_check_interval_seconds: float


def build_engine_worker_process_dependencies(
    dependencies_type: DependencyBuilder[DependencyT],
    *,
    terminate_process: ProcessTerminator,
    wait_for_cancellable_process: CancellableProcessWaiter,
    sleep: SleepFn,
    cancel_check_interval_seconds: float,
    **extra_fields: Any,
) -> DependencyT:
    return dependencies_type(
        terminate_process=terminate_process,
        wait_for_cancellable_process=wait_for_cancellable_process,
        sleep=sleep,
        cancel_check_interval_seconds=cancel_check_interval_seconds,
        **extra_fields,
    )


def build_engine_worker_process_default_factories(
    *,
    runner_dependencies_type: DependencyBuilder[Any],
    terminate_process: ProcessTerminator,
    wait_for_cancellable_process: CancellableProcessWaiter,
    sleep: SleepFn,
    cancel_check_interval_seconds: float,
    now_utc_iso: NowUtcIso,
    get_cancel_requested: CancelRequested,
    mark_completed: QueueStatusMarker,
    mark_cancelled: QueueStatusMarker,
    mark_failed: QueueStatusMarker,
    **engine_runner_dependencies: Any,
) -> dict[str, DependencyFactory]:
    return {
        "timing": lambda: EngineWorkerTimingDependencies(now_utc_iso=now_utc_iso),
        "queue": lambda: EngineWorkerQueueDependencies(
            get_cancel_requested=get_cancel_requested,
            mark_completed=mark_completed,
            mark_cancelled=mark_cancelled,
            mark_failed=mark_failed,
        ),
        "runner": lambda: build_engine_worker_process_dependencies(
            runner_dependencies_type,
            terminate_process=terminate_process,
            wait_for_cancellable_process=wait_for_cancellable_process,
            sleep=sleep,
            cancel_check_interval_seconds=cancel_check_interval_seconds,
            **engine_runner_dependencies,
        ),
    }


@dataclass(frozen=True)
class EngineWorkerOptions:
    should_cancel: Callable[[], bool] | None = None
    shutdown_requested: Callable[[], bool] | None = None
    prepare_running_job: Callable[[], None] | None = None
    register_running_job: Callable[[Any | None], None] | None = None
    worker_job_pid: int | None = None
    emit_output: bool = False


EngineContextBuilder = Callable[[Any, Any], Any]
EngineMarkRunning = Callable[[Any, Any, EngineWorkerOptions], None]
EngineJobRunner = Callable[[Any, Any, Path, EngineWorkerOptions], Any]
EngineEntryFinalizer = Callable[[Any, Any, Any, Path, EngineWorkerOptions], Any]
EngineOutcomeBuilder = Callable[[Any, Any, Any], Any]
EngineWorkerExecutionSpecFactory = Callable[[], "EngineWorkerExecutionSpec"]


@dataclass(frozen=True)
class EngineWorkerExecutionSpec:
    build_context: EngineContextBuilder
    mark_running: EngineMarkRunning
    run_job: EngineJobRunner
    finalize_entry: EngineEntryFinalizer
    build_outcome: EngineOutcomeBuilder = lambda _context, _result, finalized: finalized


def raise_if_shutdown_requested(
    context: Any,
    options: EngineWorkerOptions,
) -> None:
    if options.shutdown_requested is not None and options.shutdown_requested():
        raise WorkerShutdownRequested(context)


def raise_if_shutdown_callback_requested(
    context: Any,
    shutdown_requested: Callable[[], bool] | None,
) -> None:
    raise_if_shutdown_requested(
        context,
        EngineWorkerOptions(shutdown_requested=shutdown_requested),
    )


def queue_cancel_requested(
    queue_deps: EngineWorkerQueueDependencies,
    queue_root: str | Path,
    entry: Any,
) -> bool:
    return queue_deps.get_cancel_requested(
        str(queue_root),
        str(entry.queue_id),
        expected_entry=entry,
        expected_task_id=str(entry.task_id),
    )


def queue_cancel_callback(
    queue_deps: EngineWorkerQueueDependencies,
    queue_root: str | Path,
    entry: Any,
) -> Callable[[], bool]:
    return lambda: queue_cancel_requested(queue_deps, queue_root, entry)


def run_engine_worker_entry_with_spec(
    cfg: Any,
    entry: Any,
    *,
    queue_root: Path | None,
    spec: EngineWorkerExecutionSpec,
    options: EngineWorkerOptions | None = None,
) -> Any:
    active_options = options or EngineWorkerOptions()
    return run_engine_worker_lifecycle(
        cfg,
        entry,
        queue_root=queue_root,
        lifecycle=EngineWorkerLifecycle(
            build_context=spec.build_context,
            check_shutdown=lambda context: raise_if_shutdown_requested(
                context,
                active_options,
            ),
            mark_running=lambda cfg_obj, context: spec.mark_running(
                cfg_obj,
                context,
                active_options,
            ),
            run_job=lambda cfg_obj, context, active_queue_root: spec.run_job(
                cfg_obj,
                context,
                active_queue_root,
                active_options,
            ),
            finalize_entry=lambda cfg_obj, context, result, active_queue_root: spec.finalize_entry(
                cfg_obj,
                context,
                result,
                active_queue_root,
                active_options,
            ),
            build_outcome=spec.build_outcome,
        ),
    )


def run_engine_worker_entry_with_spec_factory_options(
    cfg: Any,
    entry: Any,
    *,
    queue_root: Path | None,
    spec_factory: EngineWorkerExecutionSpecFactory,
    should_cancel: Callable[[], bool] | None = None,
    shutdown_requested: Callable[[], bool] | None = None,
    prepare_running_job: Callable[[], None] | None = None,
    register_running_job: Callable[[Any | None], None] | None = None,
    worker_job_pid: int | None = None,
    emit_output: bool = False,
) -> Any:
    return run_engine_worker_entry_with_spec(
        cfg,
        entry,
        queue_root=queue_root,
        spec=spec_factory(),
        options=EngineWorkerOptions(
            should_cancel=should_cancel,
            shutdown_requested=shutdown_requested,
            prepare_running_job=prepare_running_job,
            register_running_job=register_running_job,
            worker_job_pid=worker_job_pid,
            emit_output=emit_output,
        ),
    )


def run_engine_worker_process_job(
    context: Any,
    *,
    options: EngineWorkerOptions,
    process_deps: EngineWorkerProcessDependencies,
    start_job: StartJob,
    finalize_job: CancellableJobFinalizer,
    build_failure_result: FailureResultBuilder,
    check_cancel_before_poll: bool = False,
    should_reraise_exception: Callable[[Exception], bool] | None = None,
) -> Any:
    def raise_shutdown(_running: Any) -> None:
        raise WorkerShutdownRequested(context)

    return run_cancellable_engine_process(
        prepare_running_job=options.prepare_running_job,
        start_job=start_job,
        finalize_job=finalize_job,
        terminate_process=process_deps.terminate_process,
        build_failure_result=build_failure_result,
        wait_for_cancellable_process=process_deps.wait_for_cancellable_process,
        should_cancel=options.should_cancel,
        shutdown_requested=options.shutdown_requested,
        on_shutdown=raise_shutdown,
        sleep=process_deps.sleep,
        poll_interval_seconds=process_deps.cancel_check_interval_seconds,
        check_cancel_before_poll=check_cancel_before_poll,
        register_running_job=options.register_running_job,
        should_reraise_exception=should_reraise_exception
        or (lambda exc: isinstance(exc, WorkerShutdownRequested)),
    )


__all__ = [
    "EngineWorkerExecutionSpec",
    "EngineWorkerExecutionSpecFactory",
    "EngineWorkerProcessDependencies",
    "EngineWorkerQueueDependencies",
    "EngineWorkerTimingDependencies",
    "EngineWorkerOptions",
    "WorkerShutdownRequested",
    "build_engine_worker_process_default_factories",
    "build_engine_worker_process_dependencies",
    "queue_cancel_callback",
    "queue_cancel_requested",
    "raise_if_shutdown_callback_requested",
    "raise_if_shutdown_requested",
    "run_engine_worker_entry_with_spec",
    "run_engine_worker_entry_with_spec_factory_options",
    "run_engine_worker_process_job",
]
