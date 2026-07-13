from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from .cancellable import run_cancellable_engine_process
from .engine.lifecycle import EngineWorkerLifecycle, run_engine_worker_lifecycle

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


class CancellableJobFinalizer(Protocol):
    def __call__(
        self,
        running: Any,
        *,
        forced_status: str | None = None,
        forced_reason: str | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class InternalWorkerTimingDependencies:
    now_utc_iso: NowUtcIso


@dataclass(frozen=True)
class InternalWorkerQueueDependencies:
    get_cancel_requested: CancelRequested
    mark_completed: QueueStatusMarker
    mark_cancelled: QueueStatusMarker
    mark_failed: QueueStatusMarker


@dataclass(frozen=True)
class InternalWorkerProcessDependencies:
    terminate_process: ProcessTerminator
    wait_for_cancellable_process: CancellableProcessWaiter
    sleep: SleepFn
    cancel_check_interval_seconds: float


def build_internal_worker_timing_dependencies(
    dependencies_type: DependencyBuilder[DependencyT],
    *,
    now_utc_iso: NowUtcIso,
) -> DependencyT:
    return dependencies_type(now_utc_iso=now_utc_iso)


def build_internal_worker_queue_dependencies(
    dependencies_type: DependencyBuilder[DependencyT],
    *,
    get_cancel_requested: CancelRequested,
    mark_completed: QueueStatusMarker,
    mark_cancelled: QueueStatusMarker,
    mark_failed: QueueStatusMarker,
) -> DependencyT:
    return dependencies_type(
        get_cancel_requested=get_cancel_requested,
        mark_completed=mark_completed,
        mark_cancelled=mark_cancelled,
        mark_failed=mark_failed,
    )


def build_internal_worker_default_factories(
    *,
    timing_dependencies_type: DependencyBuilder[Any],
    queue_dependencies_type: DependencyBuilder[Any],
    runner_factory: DependencyFactory,
    now_utc_iso: NowUtcIso,
    get_cancel_requested: CancelRequested,
    mark_completed: QueueStatusMarker,
    mark_cancelled: QueueStatusMarker,
    mark_failed: QueueStatusMarker,
) -> dict[str, DependencyFactory]:
    return {
        "timing": lambda: build_internal_worker_timing_dependencies(
            timing_dependencies_type,
            now_utc_iso=now_utc_iso,
        ),
        "queue": lambda: build_internal_worker_queue_dependencies(
            queue_dependencies_type,
            get_cancel_requested=get_cancel_requested,
            mark_completed=mark_completed,
            mark_cancelled=mark_cancelled,
            mark_failed=mark_failed,
        ),
        "runner": runner_factory,
    }


def build_internal_worker_process_default_factories(
    *,
    timing_dependencies_type: DependencyBuilder[Any],
    queue_dependencies_type: DependencyBuilder[Any],
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
    def runner_factory() -> Any:
        return build_internal_worker_process_dependencies(
            runner_dependencies_type,
            terminate_process=terminate_process,
            wait_for_cancellable_process=wait_for_cancellable_process,
            sleep=sleep,
            cancel_check_interval_seconds=cancel_check_interval_seconds,
            **engine_runner_dependencies,
        )

    return build_internal_worker_default_factories(
        timing_dependencies_type=timing_dependencies_type,
        queue_dependencies_type=queue_dependencies_type,
        runner_factory=runner_factory,
        now_utc_iso=now_utc_iso,
        get_cancel_requested=get_cancel_requested,
        mark_completed=mark_completed,
        mark_cancelled=mark_cancelled,
        mark_failed=mark_failed,
    )


def build_internal_worker_process_dependencies(
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


@dataclass(frozen=True)
class InternalWorkerOptions:
    should_cancel: Callable[[], bool] | None = None
    shutdown_requested: Callable[[], bool] | None = None
    prepare_running_job: Callable[[], None] | None = None
    register_running_job: Callable[[Any | None], None] | None = None
    worker_job_pid: int | None = None
    emit_output: bool = False


EngineContextBuilder = Callable[[Any, Any], Any]
EngineMarkRunning = Callable[[Any, Any, InternalWorkerOptions], None]
EngineJobRunner = Callable[[Any, Any, Path, InternalWorkerOptions], Any]
EngineEntryFinalizer = Callable[[Any, Any, Any, Path, InternalWorkerOptions], Any]
EngineOutcomeBuilder = Callable[[Any, Any, Any], Any]
EngineShutdownChecker = Callable[[Any, InternalWorkerOptions], None]
EngineWorkerExecutionSpecFactory = Callable[[], "InternalEngineWorkerExecutionSpec"]


@dataclass(frozen=True)
class InternalEngineWorkerAdapter:
    build_context: EngineContextBuilder
    mark_running: EngineMarkRunning
    run_job: EngineJobRunner
    finalize_entry: EngineEntryFinalizer
    build_outcome: EngineOutcomeBuilder = lambda _context, _result, finalized: finalized
    check_shutdown: EngineShutdownChecker | None = None


@dataclass(frozen=True)
class InternalEngineWorkerHooks:
    build_context: EngineContextBuilder
    mark_running: EngineMarkRunning
    run_job: EngineJobRunner
    finalize_entry: EngineEntryFinalizer
    shutdown_exception_type: type[BaseException]
    build_outcome: EngineOutcomeBuilder = lambda _context, _result, finalized: finalized


@dataclass(frozen=True)
class InternalEngineWorkerExecutionSpec:
    build_context: EngineContextBuilder
    mark_running: EngineMarkRunning
    run_job: EngineJobRunner
    finalize_entry: EngineEntryFinalizer
    shutdown_exception_type: type[BaseException]
    build_outcome: EngineOutcomeBuilder = lambda _context, _result, finalized: finalized

    def hooks(self) -> InternalEngineWorkerHooks:
        return InternalEngineWorkerHooks(
            build_context=self.build_context,
            mark_running=self.mark_running,
            run_job=self.run_job,
            finalize_entry=self.finalize_entry,
            shutdown_exception_type=self.shutdown_exception_type,
            build_outcome=self.build_outcome,
        )


def build_internal_engine_worker_execution_spec(
    *,
    build_context: EngineContextBuilder,
    mark_running: EngineMarkRunning,
    run_job: EngineJobRunner,
    finalize_entry: EngineEntryFinalizer,
    shutdown_exception_type: type[BaseException],
    build_outcome: EngineOutcomeBuilder = lambda _context, _result, finalized: finalized,
) -> InternalEngineWorkerExecutionSpec:
    return InternalEngineWorkerExecutionSpec(
        build_context=build_context,
        mark_running=mark_running,
        run_job=run_job,
        finalize_entry=finalize_entry,
        shutdown_exception_type=shutdown_exception_type,
        build_outcome=build_outcome,
    )


def build_internal_engine_worker_adapter(
    *,
    build_context: EngineContextBuilder,
    mark_running: EngineMarkRunning,
    run_job: EngineJobRunner,
    finalize_entry: EngineEntryFinalizer,
    shutdown_exception_type: type[BaseException],
    build_outcome: EngineOutcomeBuilder = lambda _context, _result, finalized: finalized,
) -> InternalEngineWorkerAdapter:
    return InternalEngineWorkerAdapter(
        build_context=build_context,
        mark_running=mark_running,
        check_shutdown=lambda context, options: raise_if_shutdown_requested(
            context,
            options,
            shutdown_exception_type=shutdown_exception_type,
        ),
        run_job=run_job,
        finalize_entry=finalize_entry,
        build_outcome=build_outcome,
    )


def build_internal_engine_worker_adapter_from_hooks(
    hooks: InternalEngineWorkerHooks,
) -> InternalEngineWorkerAdapter:
    return build_internal_engine_worker_adapter(
        build_context=hooks.build_context,
        mark_running=hooks.mark_running,
        run_job=hooks.run_job,
        finalize_entry=hooks.finalize_entry,
        shutdown_exception_type=hooks.shutdown_exception_type,
        build_outcome=hooks.build_outcome,
    )


def build_internal_engine_worker_adapter_from_spec(
    spec: InternalEngineWorkerExecutionSpec,
) -> InternalEngineWorkerAdapter:
    return build_internal_engine_worker_adapter_from_hooks(spec.hooks())


def raise_if_shutdown_requested(
    context: Any,
    options: InternalWorkerOptions,
    *,
    shutdown_exception_type: type[BaseException],
) -> None:
    if options.shutdown_requested is not None and options.shutdown_requested():
        raise shutdown_exception_type(context)


def raise_if_shutdown_callback_requested(
    context: Any,
    shutdown_requested: Callable[[], bool] | None,
    *,
    shutdown_exception_type: type[BaseException],
) -> None:
    raise_if_shutdown_requested(
        context,
        InternalWorkerOptions(shutdown_requested=shutdown_requested),
        shutdown_exception_type=shutdown_exception_type,
    )


def queue_cancel_requested(
    queue_deps: InternalWorkerQueueDependencies,
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
    queue_deps: InternalWorkerQueueDependencies,
    queue_root: str | Path,
    entry: Any,
) -> Callable[[], bool]:
    return lambda: queue_cancel_requested(queue_deps, queue_root, entry)


def run_internal_engine_worker_entry(
    cfg: Any,
    entry: Any,
    *,
    queue_root: Path | None,
    adapter: InternalEngineWorkerAdapter,
    options: InternalWorkerOptions | None = None,
) -> Any:
    active_options = options or InternalWorkerOptions()
    check_shutdown = adapter.check_shutdown
    return run_engine_worker_lifecycle(
        cfg,
        entry,
        queue_root=queue_root,
        lifecycle=EngineWorkerLifecycle(
            build_context=adapter.build_context,
            check_shutdown=(
                None
                if check_shutdown is None
                else lambda context: check_shutdown(context, active_options)
            ),
            mark_running=lambda cfg_obj, context: adapter.mark_running(
                cfg_obj,
                context,
                active_options,
            ),
            run_job=lambda cfg_obj, context, active_queue_root: adapter.run_job(
                cfg_obj,
                context,
                active_queue_root,
                active_options,
            ),
            finalize_entry=lambda cfg_obj, context, result, active_queue_root: (
                adapter.finalize_entry(
                    cfg_obj,
                    context,
                    result,
                    active_queue_root,
                    active_options,
                )
            ),
            build_outcome=adapter.build_outcome,
        ),
    )


def run_internal_engine_worker_entry_with_hooks(
    cfg: Any,
    entry: Any,
    *,
    queue_root: Path | None,
    hooks: InternalEngineWorkerHooks,
    options: InternalWorkerOptions | None = None,
) -> Any:
    return run_internal_engine_worker_entry(
        cfg,
        entry,
        queue_root=queue_root,
        adapter=build_internal_engine_worker_adapter_from_hooks(hooks),
        options=options,
    )


def run_internal_engine_worker_entry_with_spec(
    cfg: Any,
    entry: Any,
    *,
    queue_root: Path | None,
    spec: InternalEngineWorkerExecutionSpec,
    options: InternalWorkerOptions | None = None,
) -> Any:
    return run_internal_engine_worker_entry(
        cfg,
        entry,
        queue_root=queue_root,
        adapter=build_internal_engine_worker_adapter_from_spec(spec),
        options=options,
    )


def run_internal_engine_worker_entry_with_spec_options(
    cfg: Any,
    entry: Any,
    *,
    queue_root: Path | None,
    spec: InternalEngineWorkerExecutionSpec,
    should_cancel: Callable[[], bool] | None = None,
    shutdown_requested: Callable[[], bool] | None = None,
    prepare_running_job: Callable[[], None] | None = None,
    register_running_job: Callable[[Any | None], None] | None = None,
    worker_job_pid: int | None = None,
    emit_output: bool = False,
) -> Any:
    return run_internal_engine_worker_entry_with_spec(
        cfg,
        entry,
        queue_root=queue_root,
        spec=spec,
        options=InternalWorkerOptions(
            should_cancel=should_cancel,
            shutdown_requested=shutdown_requested,
            prepare_running_job=prepare_running_job,
            register_running_job=register_running_job,
            worker_job_pid=worker_job_pid,
            emit_output=emit_output,
        ),
    )


def run_internal_engine_worker_entry_with_spec_factory_options(
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
    return run_internal_engine_worker_entry_with_spec_options(
        cfg,
        entry,
        queue_root=queue_root,
        spec=spec_factory(),
        should_cancel=should_cancel,
        shutdown_requested=shutdown_requested,
        prepare_running_job=prepare_running_job,
        register_running_job=register_running_job,
        worker_job_pid=worker_job_pid,
        emit_output=emit_output,
    )


def run_internal_cancellable_engine_process(
    context: Any,
    *,
    options: InternalWorkerOptions,
    shutdown_exception_type: type[BaseException],
    start_job: StartJob,
    finalize_job: CancellableJobFinalizer,
    terminate_process: ProcessTerminator,
    build_failure_result: FailureResultBuilder,
    wait_for_cancellable_process: CancellableProcessWaiter | None = None,
    sleep: SleepFn | None = None,
    poll_interval_seconds: float = 1.0,
    check_cancel_before_poll: bool = False,
    should_reraise_exception: Callable[[Exception], bool] | None = None,
) -> Any:
    def raise_shutdown(_running: Any) -> None:
        raise shutdown_exception_type(context)

    return run_cancellable_engine_process(
        prepare_running_job=options.prepare_running_job,
        start_job=start_job,
        finalize_job=finalize_job,
        terminate_process=terminate_process,
        build_failure_result=build_failure_result,
        wait_for_cancellable_process=wait_for_cancellable_process,
        should_cancel=options.should_cancel,
        shutdown_requested=options.shutdown_requested,
        on_shutdown=raise_shutdown,
        sleep=sleep,
        poll_interval_seconds=poll_interval_seconds,
        check_cancel_before_poll=check_cancel_before_poll,
        register_running_job=options.register_running_job,
        should_reraise_exception=should_reraise_exception
        or (lambda exc: isinstance(exc, shutdown_exception_type)),
    )


def run_internal_worker_process_job(
    context: Any,
    *,
    options: InternalWorkerOptions,
    process_deps: InternalWorkerProcessDependencies,
    shutdown_exception_type: type[BaseException],
    start_job: StartJob,
    finalize_job: CancellableJobFinalizer,
    build_failure_result: FailureResultBuilder,
    check_cancel_before_poll: bool = False,
    should_reraise_exception: Callable[[Exception], bool] | None = None,
) -> Any:
    return run_internal_cancellable_engine_process(
        context,
        options=options,
        shutdown_exception_type=shutdown_exception_type,
        start_job=start_job,
        finalize_job=finalize_job,
        terminate_process=process_deps.terminate_process,
        build_failure_result=build_failure_result,
        wait_for_cancellable_process=process_deps.wait_for_cancellable_process,
        sleep=process_deps.sleep,
        poll_interval_seconds=process_deps.cancel_check_interval_seconds,
        check_cancel_before_poll=check_cancel_before_poll,
        should_reraise_exception=should_reraise_exception,
    )


__all__ = [
    "InternalEngineWorkerAdapter",
    "InternalEngineWorkerExecutionSpec",
    "EngineWorkerExecutionSpecFactory",
    "InternalEngineWorkerHooks",
    "InternalWorkerProcessDependencies",
    "InternalWorkerQueueDependencies",
    "InternalWorkerTimingDependencies",
    "InternalWorkerOptions",
    "build_internal_worker_default_factories",
    "build_internal_worker_process_default_factories",
    "build_internal_worker_process_dependencies",
    "build_internal_worker_queue_dependencies",
    "build_internal_worker_timing_dependencies",
    "build_internal_engine_worker_execution_spec",
    "build_internal_engine_worker_adapter",
    "build_internal_engine_worker_adapter_from_spec",
    "build_internal_engine_worker_adapter_from_hooks",
    "queue_cancel_callback",
    "queue_cancel_requested",
    "raise_if_shutdown_callback_requested",
    "raise_if_shutdown_requested",
    "run_internal_cancellable_engine_process",
    "run_internal_engine_worker_entry",
    "run_internal_engine_worker_entry_with_spec",
    "run_internal_engine_worker_entry_with_spec_factory_options",
    "run_internal_engine_worker_entry_with_spec_options",
    "run_internal_engine_worker_entry_with_hooks",
    "run_internal_worker_process_job",
]
