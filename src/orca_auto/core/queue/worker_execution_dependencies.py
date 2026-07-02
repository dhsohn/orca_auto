from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import child_entrypoint as _child_entrypoint
from .child_execution import build_queue_entry_lookup as _build_queue_entry_lookup
from .dependencies import build_dependency_container
from .internal_worker import (
    build_internal_worker_process_default_factories,
    build_internal_worker_process_dependencies,
    build_internal_worker_queue_dependencies,
    build_internal_worker_timing_dependencies,
)

DependencyFactory = Callable[[], Any]


@dataclass(frozen=True)
class WorkerConfigDependencies:
    load_config: Callable[..., Any]
    queue_entry_by_id: Callable[[Path | str, str], Any | None]


@dataclass(frozen=True)
class WorkerAdmissionDependencies:
    activate_reserved_slot: Callable[..., Any]
    release_slot: Callable[..., Any]


@dataclass(frozen=True)
class WorkerProcessDependencyCallbacks:
    terminate_process: Callable[..., Any]
    wait_for_cancellable_process: Callable[..., Any]
    sleep: Callable[..., Any]
    now_utc_iso: Callable[..., Any]
    get_cancel_requested: Callable[..., Any]
    mark_completed: Callable[..., Any]
    mark_cancelled: Callable[..., Any]
    mark_failed: Callable[..., Any]
    engine_runner_dependencies: Mapping[str, Any]


def build_worker_process_dependency_callbacks(
    *,
    terminate_process: Callable[..., Any],
    wait_for_cancellable_process: Callable[..., Any],
    sleep: Callable[..., Any],
    now_utc_iso: Callable[..., Any],
    get_cancel_requested: Callable[..., Any],
    mark_completed: Callable[..., Any],
    mark_cancelled: Callable[..., Any],
    mark_failed: Callable[..., Any],
    engine_runner_dependencies: Mapping[str, Any],
) -> WorkerProcessDependencyCallbacks:
    return WorkerProcessDependencyCallbacks(
        terminate_process=terminate_process,
        wait_for_cancellable_process=wait_for_cancellable_process,
        sleep=sleep,
        now_utc_iso=now_utc_iso,
        get_cancel_requested=get_cancel_requested,
        mark_completed=mark_completed,
        mark_cancelled=mark_cancelled,
        mark_failed=mark_failed,
        engine_runner_dependencies=engine_runner_dependencies,
    )


def worker_process_dependency_callbacks_from_attrs(
    source: Any,
    *,
    engine_runner_dependency_names: tuple[str, ...],
    terminate_process_name: str = "terminate_process",
    wait_for_cancellable_process_name: str = "wait_for_cancellable_process",
    sleep_name: str = "sleep",
    now_utc_iso_name: str = "now_utc_iso",
    get_cancel_requested_name: str = "get_cancel_requested",
    mark_completed_name: str = "mark_completed",
    mark_cancelled_name: str = "mark_cancelled",
    mark_failed_name: str = "mark_failed",
) -> WorkerProcessDependencyCallbacks:
    return build_worker_process_dependency_callbacks(
        terminate_process=getattr(source, terminate_process_name),
        wait_for_cancellable_process=getattr(source, wait_for_cancellable_process_name),
        sleep=getattr(source, sleep_name),
        now_utc_iso=getattr(source, now_utc_iso_name),
        get_cancel_requested=getattr(source, get_cancel_requested_name),
        mark_completed=getattr(source, mark_completed_name),
        mark_cancelled=getattr(source, mark_cancelled_name),
        mark_failed=getattr(source, mark_failed_name),
        engine_runner_dependencies={
            name: getattr(source, name) for name in engine_runner_dependency_names
        },
    )


def worker_process_dependency_callback_kwargs(
    callbacks: WorkerProcessDependencyCallbacks,
    *,
    include_engine_runner_dependencies: bool = False,
) -> dict[str, Any]:
    kwargs = {
        "terminate_process": callbacks.terminate_process,
        "wait_for_cancellable_process": callbacks.wait_for_cancellable_process,
        "sleep": callbacks.sleep,
        "now_utc_iso": callbacks.now_utc_iso,
        "get_cancel_requested": callbacks.get_cancel_requested,
        "mark_completed": callbacks.mark_completed,
        "mark_cancelled": callbacks.mark_cancelled,
        "mark_failed": callbacks.mark_failed,
    }
    if include_engine_runner_dependencies:
        kwargs.update(callbacks.engine_runner_dependencies)
    return kwargs


def queue_entry_by_id(
    queue_root: Path | str,
    queue_id: str,
    *,
    list_queue_fn: Callable[..., Any],
) -> Any | None:
    return _child_entrypoint.queue_entry_by_id(
        queue_root,
        queue_id,
        list_queue_fn=list_queue_fn,
    )


def build_queue_entry_lookup(
    *,
    list_queue_fn: Callable[[str | Path], Any],
    coerce_root_to_path: bool = False,
) -> Callable[[str | Path, str], Any | None]:
    return _build_queue_entry_lookup(
        list_queue_fn=list_queue_fn,
        coerce_root_to_path=coerce_root_to_path,
    )


def build_worker_config_dependencies(
    *,
    load_config: Callable[..., Any],
    queue_entry_by_id_fn: Callable[[Path | str, str], Any | None],
) -> WorkerConfigDependencies:
    return WorkerConfigDependencies(
        load_config=load_config,
        queue_entry_by_id=queue_entry_by_id_fn,
    )


def build_worker_admission_dependencies(
    *,
    activate_reserved_slot: Callable[..., Any],
    release_slot: Callable[..., Any],
) -> WorkerAdmissionDependencies:
    return WorkerAdmissionDependencies(
        activate_reserved_slot=activate_reserved_slot,
        release_slot=release_slot,
    )


def build_worker_execution_dependencies_from_groups(
    dependencies_type: Callable[..., Any],
    groups: Mapping[str, Any],
    *,
    execute_queue_entry_fn: Callable[..., Any] | None = None,
) -> Any:
    resolved = {name: value for name, value in groups.items() if value is not None}
    return dependencies_type(
        **resolved,
        execute_queue_entry=execute_queue_entry_fn,
    )


def build_worker_process_default_factories(
    *,
    config_factory: DependencyFactory,
    admission_factory: DependencyFactory,
    timing_dependencies_type: Callable[..., Any],
    queue_dependencies_type: Callable[..., Any],
    runner_dependencies_type: Callable[..., Any],
    terminate_process: Callable[..., Any],
    wait_for_cancellable_process: Callable[..., Any],
    sleep: Callable[..., Any],
    cancel_check_interval_seconds: float,
    now_utc_iso: Callable[..., Any],
    get_cancel_requested: Callable[..., Any],
    mark_completed: Callable[..., Any],
    mark_cancelled: Callable[..., Any],
    mark_failed: Callable[..., Any],
    engine_runner_dependencies: Mapping[str, Any],
) -> dict[str, DependencyFactory]:
    return {
        "config": config_factory,
        "admission": admission_factory,
        **build_internal_worker_process_default_factories(
            timing_dependencies_type=timing_dependencies_type,
            queue_dependencies_type=queue_dependencies_type,
            runner_dependencies_type=runner_dependencies_type,
            terminate_process=terminate_process,
            wait_for_cancellable_process=wait_for_cancellable_process,
            sleep=sleep,
            cancel_check_interval_seconds=cancel_check_interval_seconds,
            now_utc_iso=now_utc_iso,
            get_cancel_requested=get_cancel_requested,
            mark_completed=mark_completed,
            mark_cancelled=mark_cancelled,
            mark_failed=mark_failed,
            **dict(engine_runner_dependencies),
        ),
    }


def build_worker_process_default_factories_from_callbacks(
    callbacks: WorkerProcessDependencyCallbacks,
    *,
    config_factory: DependencyFactory,
    admission_factory: DependencyFactory,
    timing_dependencies_type: Callable[..., Any],
    queue_dependencies_type: Callable[..., Any],
    runner_dependencies_type: Callable[..., Any],
    cancel_check_interval_seconds: float,
) -> dict[str, DependencyFactory]:
    return build_worker_process_default_factories(
        config_factory=config_factory,
        admission_factory=admission_factory,
        timing_dependencies_type=timing_dependencies_type,
        queue_dependencies_type=queue_dependencies_type,
        runner_dependencies_type=runner_dependencies_type,
        terminate_process=callbacks.terminate_process,
        wait_for_cancellable_process=callbacks.wait_for_cancellable_process,
        sleep=callbacks.sleep,
        cancel_check_interval_seconds=cancel_check_interval_seconds,
        now_utc_iso=callbacks.now_utc_iso,
        get_cancel_requested=callbacks.get_cancel_requested,
        mark_completed=callbacks.mark_completed,
        mark_cancelled=callbacks.mark_cancelled,
        mark_failed=callbacks.mark_failed,
        engine_runner_dependencies=callbacks.engine_runner_dependencies,
    )


def build_worker_process_dependency_groups(
    callbacks: WorkerProcessDependencyCallbacks,
    *,
    timing_dependencies_type: Callable[..., Any],
    queue_dependencies_type: Callable[..., Any],
    runner_dependencies_type: Callable[..., Any],
    cancel_check_interval_seconds: float,
) -> dict[str, Any]:
    return {
        "timing": build_internal_worker_timing_dependencies(
            timing_dependencies_type,
            now_utc_iso=callbacks.now_utc_iso,
        ),
        "queue": build_internal_worker_queue_dependencies(
            queue_dependencies_type,
            get_cancel_requested=callbacks.get_cancel_requested,
            mark_completed=callbacks.mark_completed,
            mark_cancelled=callbacks.mark_cancelled,
            mark_failed=callbacks.mark_failed,
        ),
        "runner": build_internal_worker_process_dependencies(
            runner_dependencies_type,
            terminate_process=callbacks.terminate_process,
            wait_for_cancellable_process=callbacks.wait_for_cancellable_process,
            sleep=callbacks.sleep,
            cancel_check_interval_seconds=cancel_check_interval_seconds,
            **dict(callbacks.engine_runner_dependencies),
        ),
    }


def build_worker_execution_dependency_container(
    container_builder: Callable[..., Any],
    overrides: Mapping[str, Any],
    default_factories: Mapping[str, DependencyFactory],
    *,
    execute_queue_entry_fn: Callable[..., Any] | None = None,
) -> Any:
    return build_dependency_container(
        container_builder,
        overrides,
        default_factories,
        extra_fields={"execute_queue_entry_fn": execute_queue_entry_fn},
    )


def run_worker_child_entrypoint(
    worker_child: Any,
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    admission_token: str | None = None,
    load_config_fn: Callable[..., Any],
    find_queue_entry_fn: Callable[..., Any],
    admission_root_fn: Callable[[Any], str | Path],
    release_slot_fn: Callable[..., Any],
    install_shutdown_signal_handlers_fn: Callable[..., Any],
    process_dequeued_entry_fn: Callable[..., Any],
    dependencies_fn: Callable[[], Any],
    requeue_running_entry_fn: Callable[..., Any],
    mark_recovery_pending_context_fn: Callable[..., Any],
    process_dequeued_entry_kwargs: Mapping[str, Any] | None = None,
) -> int:
    kwargs: dict[str, Any] = {
        "config_path": config_path,
        "queue_root": queue_root,
        "queue_id": queue_id,
        "admission_token": admission_token,
        "load_config_fn": load_config_fn,
        "find_queue_entry_fn": find_queue_entry_fn,
        "admission_root_fn": admission_root_fn,
        "release_slot_fn": release_slot_fn,
        "install_signal_handlers_fn": worker_child.shutdown_signal_handler_installer(
            install_shutdown_signal_handlers_fn,
        ),
        "process_dequeued_entry_fn": process_dequeued_entry_fn,
        "dependencies_fn": dependencies_fn,
        "requeue_running_entry_fn": requeue_running_entry_fn,
        "mark_recovery_pending_context_fn": mark_recovery_pending_context_fn,
    }
    if process_dequeued_entry_kwargs is not None:
        kwargs["process_dequeued_entry_kwargs"] = process_dequeued_entry_kwargs
    return int(worker_child.run_worker_child_job(**kwargs))


def run_worker_child_entrypoint_with_dependencies(
    worker_child: Any,
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    dependencies: Any,
    admission_root_fn: Callable[[Any], str | Path],
    install_shutdown_signal_handlers_fn: Callable[..., Any],
    process_dequeued_entry_fn: Callable[..., Any],
    requeue_running_entry_fn: Callable[..., Any],
    mark_recovery_pending_context_fn: Callable[..., Any],
    admission_token: str | None = None,
    process_dequeued_entry_kwargs: Mapping[str, Any] | None = None,
) -> int:
    return run_worker_child_entrypoint(
        worker_child,
        config_path=config_path,
        queue_root=queue_root,
        queue_id=queue_id,
        admission_token=admission_token,
        load_config_fn=dependencies.config.load_config,
        find_queue_entry_fn=dependencies.config.queue_entry_by_id,
        admission_root_fn=admission_root_fn,
        release_slot_fn=dependencies.admission.release_slot,
        install_shutdown_signal_handlers_fn=install_shutdown_signal_handlers_fn,
        process_dequeued_entry_fn=process_dequeued_entry_fn,
        dependencies_fn=lambda: dependencies,
        requeue_running_entry_fn=requeue_running_entry_fn,
        mark_recovery_pending_context_fn=mark_recovery_pending_context_fn,
        process_dequeued_entry_kwargs=process_dequeued_entry_kwargs,
    )


__all__ = [
    "WorkerAdmissionDependencies",
    "WorkerConfigDependencies",
    "WorkerProcessDependencyCallbacks",
    "build_queue_entry_lookup",
    "build_worker_admission_dependencies",
    "build_worker_config_dependencies",
    "build_worker_execution_dependencies_from_groups",
    "build_worker_execution_dependency_container",
    "build_worker_process_dependency_callbacks",
    "build_worker_process_dependency_groups",
    "build_worker_process_default_factories",
    "build_worker_process_default_factories_from_callbacks",
    "queue_entry_by_id",
    "run_worker_child_entrypoint",
    "run_worker_child_entrypoint_with_dependencies",
    "worker_process_dependency_callback_kwargs",
    "worker_process_dependency_callbacks_from_attrs",
]
