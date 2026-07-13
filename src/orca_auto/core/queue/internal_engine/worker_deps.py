from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .runtime import InternalEngineQueueRuntime

SlotReleaser = Callable[[str | Path, str], object]
BackgroundProcessStarter = Callable[[list[str]], Any]
DefaultConfigPath = Callable[[], str]
ProcessTerminator = Callable[[Any], bool]
WorkerStartErrorHandler = Callable[[Any, Path, Any, str, OSError], None]
CompletedJobFinalizer = Callable[[Any, str, Any, int], None]
WorkerStateReconciler = Callable[[Any], None]
QueueLister = Callable[[Any], list[Any]]
SlotLister = Callable[[Any], list[Any]]
StaleSlotReconciler = Callable[[Any], Any]
AdmissionSlotReserver = Callable[[Any], str | None]
QueueEntryFinder = Callable[[Any, str], Any | None]
ConfigLoader = Callable[[Any], Any]
WorkerPidReader = Callable[[Path], int | None]
WorkerProcessStartedHook = Callable[[Any, Path, Any, Any, str], bool]
ShutdownRunningJob = Callable[[Any, str, Any], Any]
BeforeShutdownAll = Callable[[Any, int], Any]


class SlotReserver(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> str | None: ...


class WorkerChildCommandBuilder(Protocol):
    def __call__(
        self,
        *,
        config_path: str,
        queue_root: str | Path,
        queue_id: str,
        admission_root: str | Path | None = None,
        admission_token: str | None = None,
    ) -> list[str]: ...


class ConfigPathForWorker(Protocol):
    def __call__(self, args: Any, *, default_config_path_fn: DefaultConfigPath) -> str: ...


class ReservedSlotActivator(Protocol):
    def __call__(
        self,
        admission_root: str | Path,
        admission_token: str,
        **metadata: Any,
    ) -> object | None: ...


class QueueStatusMarker(Protocol):
    def __call__(self, root: str | Path, queue_id: str, **kwargs: Any) -> Any: ...


class ChildExitFinalizer(Protocol):
    def __call__(self, worker: Any, job: Any, *, rc: int) -> Any: ...


class BackgroundJobProcessStarter(Protocol):
    def __call__(
        self,
        *,
        config_path: str,
        queue_root: Path,
        entry: Any,
        admission_root: str | Path,
        admission_token: str,
    ) -> Any: ...


class OrphanedChildQueueReconciler(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


class RunningEntryRequeuer(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


class RecoveryPendingMarker(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


class WorkerFactory(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


def _noop_callback(*_args: Any, **_kwargs: Any) -> None:
    return None


def _empty_default_config_path() -> str:
    return ""


def _config_path_from_args(args: Any, *, default_config_path_fn: DefaultConfigPath) -> str:
    return str(getattr(args, "config", "") or default_config_path_fn())


_CallbackSupplier = Callable[[], Callable[..., Any]]


@dataclass(frozen=True)
class InternalEngineQueueWorkerFacadeCallbacks:
    release_slot: SlotReleaser
    reserve_slot: SlotReserver
    start_background_process: BackgroundProcessStarter
    build_worker_child_command: WorkerChildCommandBuilder
    activate_reserved_slot: ReservedSlotActivator
    terminate_process: ProcessTerminator
    mark_failed: QueueStatusMarker
    handle_worker_start_error: WorkerStartErrorHandler
    finalize_completed_job: CompletedJobFinalizer
    finalize_child_exit: ChildExitFinalizer
    reconcile_worker_state: WorkerStateReconciler
    list_queue: QueueLister
    list_slots: SlotLister
    reconcile_stale_slots: StaleSlotReconciler
    mark_cancelled: QueueStatusMarker
    requeue_running_entry: RunningEntryRequeuer
    config_path_for_worker: ConfigPathForWorker = _config_path_from_args
    default_config_path: DefaultConfigPath = _empty_default_config_path
    reconcile_orphaned_child_queue_entries: OrphanedChildQueueReconciler = _noop_callback
    mark_recovery_pending: RecoveryPendingMarker = _noop_callback
    try_reserve_admission_slot: AdmissionSlotReserver | None = None
    start_background_job_process: BackgroundJobProcessStarter | None = None
    find_queue_entry: QueueEntryFinder | None = None
    load_config: ConfigLoader | None = None
    read_worker_pid: WorkerPidReader | None = None
    worker_class: WorkerFactory | None = None
    on_worker_process_started: WorkerProcessStartedHook | None = None
    shutdown_running_job: ShutdownRunningJob | None = None
    before_shutdown_all: BeforeShutdownAll | None = None


@dataclass(frozen=True)
class InternalEngineQueueWorkerFacadeBindings:
    """Late-bound callback suppliers for import-time runtime facades."""

    release_slot: _CallbackSupplier
    reserve_slot: _CallbackSupplier
    start_background_process: _CallbackSupplier
    build_worker_child_command: _CallbackSupplier
    activate_reserved_slot: _CallbackSupplier
    terminate_process: _CallbackSupplier
    mark_failed: _CallbackSupplier
    handle_worker_start_error: _CallbackSupplier
    finalize_completed_job: _CallbackSupplier
    finalize_child_exit: _CallbackSupplier
    reconcile_worker_state: _CallbackSupplier
    list_queue: _CallbackSupplier
    list_slots: _CallbackSupplier
    reconcile_stale_slots: _CallbackSupplier
    mark_cancelled: _CallbackSupplier
    requeue_running_entry: _CallbackSupplier
    config_path_for_worker: _CallbackSupplier | None = None
    default_config_path: _CallbackSupplier | None = None
    reconcile_orphaned_child_queue_entries: _CallbackSupplier | None = None
    mark_recovery_pending: _CallbackSupplier | None = None
    try_reserve_admission_slot: _CallbackSupplier | None = None
    start_background_job_process: _CallbackSupplier | None = None
    find_queue_entry: _CallbackSupplier | None = None
    load_config: _CallbackSupplier | None = None
    read_worker_pid: _CallbackSupplier | None = None
    worker_class: _CallbackSupplier | None = None
    on_worker_process_started: _CallbackSupplier | None = None
    shutdown_running_job: _CallbackSupplier | None = None
    before_shutdown_all: _CallbackSupplier | None = None


def _late_optional(
    supplier: _CallbackSupplier | None,
) -> Callable[..., Any] | None:
    if supplier is None:
        return None
    return lambda *args, **kwargs: supplier()(*args, **kwargs)


def _late_config_path_for_worker(
    supplier: _CallbackSupplier | None,
) -> ConfigPathForWorker:
    if supplier is None:
        return _config_path_from_args
    callback_supplier = supplier
    return lambda args, *, default_config_path_fn: callback_supplier()(
        args,
        default_config_path_fn=default_config_path_fn,
    )


def _late_default_config_path(supplier: _CallbackSupplier | None) -> DefaultConfigPath:
    if supplier is None:
        return _empty_default_config_path
    callback_supplier = supplier
    return lambda: callback_supplier()()


def build_late_bound_internal_engine_queue_worker_facade_callbacks(
    bindings: InternalEngineQueueWorkerFacadeBindings,
) -> InternalEngineQueueWorkerFacadeCallbacks:
    return InternalEngineQueueWorkerFacadeCallbacks(
        release_slot=lambda root, token: bindings.release_slot()(root, token),
        reserve_slot=lambda *args, **kwargs: bindings.reserve_slot()(*args, **kwargs),
        start_background_process=lambda command: bindings.start_background_process()(command),
        build_worker_child_command=lambda **kwargs: bindings.build_worker_child_command()(**kwargs),
        config_path_for_worker=_late_config_path_for_worker(
            bindings.config_path_for_worker,
        ),
        default_config_path=_late_default_config_path(bindings.default_config_path),
        activate_reserved_slot=lambda *args, **kwargs: bindings.activate_reserved_slot()(
            *args,
            **kwargs,
        ),
        terminate_process=lambda process: bindings.terminate_process()(process),
        mark_failed=lambda *args, **kwargs: bindings.mark_failed()(*args, **kwargs),
        handle_worker_start_error=lambda worker, queue_root, entry, admission_token, exc: (
            bindings.handle_worker_start_error()(
                worker,
                queue_root,
                entry,
                admission_token,
                exc,
            )
        ),
        finalize_completed_job=lambda worker, queue_id, job, rc: bindings.finalize_completed_job()(
            worker, queue_id, job, rc
        ),
        finalize_child_exit=lambda worker, job, *, rc: bindings.finalize_child_exit()(
            worker, job, rc=rc
        ),
        reconcile_worker_state=lambda worker: bindings.reconcile_worker_state()(worker),
        list_queue=lambda root: bindings.list_queue()(root),
        list_slots=lambda root: bindings.list_slots()(root),
        reconcile_stale_slots=lambda root: bindings.reconcile_stale_slots()(root),
        reconcile_orphaned_child_queue_entries=(
            _late_optional(bindings.reconcile_orphaned_child_queue_entries) or _noop_callback
        ),
        mark_cancelled=lambda *args, **kwargs: bindings.mark_cancelled()(
            *args,
            **kwargs,
        ),
        requeue_running_entry=lambda *args, **kwargs: bindings.requeue_running_entry()(
            *args,
            **kwargs,
        ),
        mark_recovery_pending=(_late_optional(bindings.mark_recovery_pending) or _noop_callback),
        try_reserve_admission_slot=_late_optional(bindings.try_reserve_admission_slot),
        start_background_job_process=_late_optional(bindings.start_background_job_process),
        find_queue_entry=_late_optional(bindings.find_queue_entry),
        load_config=_late_optional(bindings.load_config),
        read_worker_pid=_late_optional(bindings.read_worker_pid),
        worker_class=_late_optional(bindings.worker_class),
        on_worker_process_started=_late_optional(bindings.on_worker_process_started),
        shutdown_running_job=_late_optional(bindings.shutdown_running_job),
        before_shutdown_all=_late_optional(bindings.before_shutdown_all),
    )


@dataclass(frozen=True)
class InternalEngineQueueWorkerDeps:
    time_module: Any
    release_slot: SlotReleaser
    reserve_slot: SlotReserver
    start_background_process: BackgroundProcessStarter
    build_worker_child_command: WorkerChildCommandBuilder
    config_path_for_worker: ConfigPathForWorker
    default_config_path: DefaultConfigPath
    activate_reserved_slot: ReservedSlotActivator
    terminate_process: ProcessTerminator
    mark_failed: QueueStatusMarker
    handle_worker_start_error: WorkerStartErrorHandler
    finalize_completed_job: CompletedJobFinalizer
    finalize_child_exit: ChildExitFinalizer
    reconcile_worker_state: WorkerStateReconciler
    list_queue: QueueLister
    list_slots: SlotLister
    reconcile_stale_slots: StaleSlotReconciler
    reconcile_orphaned_child_queue_entries: OrphanedChildQueueReconciler
    mark_cancelled: QueueStatusMarker
    requeue_running_entry: RunningEntryRequeuer
    mark_recovery_pending: RecoveryPendingMarker
    try_reserve_admission_slot: AdmissionSlotReserver | None = None
    start_background_job_process_fn: BackgroundJobProcessStarter | None = None
    find_queue_entry: QueueEntryFinder | None = None
    load_config: ConfigLoader | None = None
    read_worker_pid: WorkerPidReader | None = None
    worker_class: WorkerFactory | None = None
    on_worker_process_started: WorkerProcessStartedHook | None = None
    shutdown_running_job: ShutdownRunningJob | None = None
    before_shutdown_all: BeforeShutdownAll | None = None


def build_internal_engine_queue_worker_deps(
    callbacks: InternalEngineQueueWorkerFacadeCallbacks,
    *,
    time_module: Any = time,
) -> InternalEngineQueueWorkerDeps:
    return InternalEngineQueueWorkerDeps(
        time_module=time_module,
        release_slot=callbacks.release_slot,
        reserve_slot=callbacks.reserve_slot,
        start_background_process=callbacks.start_background_process,
        build_worker_child_command=callbacks.build_worker_child_command,
        config_path_for_worker=callbacks.config_path_for_worker,
        default_config_path=callbacks.default_config_path,
        activate_reserved_slot=callbacks.activate_reserved_slot,
        terminate_process=callbacks.terminate_process,
        mark_failed=callbacks.mark_failed,
        handle_worker_start_error=callbacks.handle_worker_start_error,
        finalize_completed_job=callbacks.finalize_completed_job,
        finalize_child_exit=callbacks.finalize_child_exit,
        reconcile_worker_state=callbacks.reconcile_worker_state,
        list_queue=callbacks.list_queue,
        list_slots=callbacks.list_slots,
        reconcile_stale_slots=callbacks.reconcile_stale_slots,
        reconcile_orphaned_child_queue_entries=(callbacks.reconcile_orphaned_child_queue_entries),
        mark_cancelled=callbacks.mark_cancelled,
        requeue_running_entry=callbacks.requeue_running_entry,
        mark_recovery_pending=callbacks.mark_recovery_pending,
        try_reserve_admission_slot=callbacks.try_reserve_admission_slot,
        start_background_job_process_fn=callbacks.start_background_job_process,
        find_queue_entry=callbacks.find_queue_entry,
        load_config=callbacks.load_config,
        read_worker_pid=callbacks.read_worker_pid,
        worker_class=callbacks.worker_class,
        on_worker_process_started=callbacks.on_worker_process_started,
        shutdown_running_job=callbacks.shutdown_running_job,
        before_shutdown_all=callbacks.before_shutdown_all,
    )


def build_late_bound_internal_engine_queue_worker_deps(
    bindings: InternalEngineQueueWorkerFacadeBindings,
    *,
    time_module: Any = time,
) -> InternalEngineQueueWorkerDeps:
    return build_internal_engine_queue_worker_deps(
        build_late_bound_internal_engine_queue_worker_facade_callbacks(bindings),
        time_module=time_module,
    )


@dataclass(frozen=True)
class InternalEngineQueueWorkerDepsResolver:
    runtime: InternalEngineQueueRuntime
    deps: InternalEngineQueueWorkerDeps

    def find_queue_entry(self, queue_root: Any, queue_id: str) -> Any | None:
        if self.deps.find_queue_entry is not None:
            entry = self.deps.find_queue_entry(queue_root, queue_id)
            return entry if entry is None or self.runtime.accepts_entry(entry) else None
        return self.runtime.queue_entry_by_id(queue_root, queue_id)

    def queue_worker_deps(
        self,
        *,
        poll_interval_seconds: int,
        start_background_job_process_fn: BackgroundJobProcessStarter,
        try_reserve_admission_slot_fn: AdmissionSlotReserver,
    ) -> Any:
        return self.runtime.child_worker_deps(
            poll_interval_seconds=poll_interval_seconds,
            time_module=self.deps.time_module,
            release_slot_fn=self.deps.release_slot,
            start_background_job_process_fn=(
                self.deps.start_background_job_process_fn or start_background_job_process_fn
            ),
            try_reserve_admission_slot_fn=(
                self.deps.try_reserve_admission_slot or try_reserve_admission_slot_fn
            ),
        )

    def try_reserve_admission_slot(self, cfg: Any) -> str | None:
        return self.runtime.reserve_admission_slot(
            cfg,
            reserve_slot_fn=self.deps.reserve_slot,
        )

    def start_background_job_process(
        self,
        *,
        config_path: str,
        queue_root: Path,
        entry: Any,
        admission_root: str | Path,
        admission_token: str,
    ) -> Any:
        return self.runtime.start_child_process(
            config_path=config_path,
            queue_root=queue_root,
            entry=entry,
            admission_root=admission_root,
            admission_token=admission_token,
            start_background_process_fn=self.deps.start_background_process,
            build_worker_child_command_fn=self.deps.build_worker_child_command,
        )

    def config_path_for_worker(self, args: Any) -> str:
        return self.deps.config_path_for_worker(
            args,
            default_config_path_fn=self.deps.default_config_path,
        )


__all__ = [
    "InternalEngineQueueWorkerFacadeBindings",
    "InternalEngineQueueWorkerFacadeCallbacks",
    "InternalEngineQueueWorkerDeps",
    "InternalEngineQueueWorkerDepsResolver",
    "build_internal_engine_queue_worker_deps",
    "build_late_bound_internal_engine_queue_worker_deps",
    "build_late_bound_internal_engine_queue_worker_facade_callbacks",
]
