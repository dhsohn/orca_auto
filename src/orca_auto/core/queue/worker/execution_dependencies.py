from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..child.execution import build_queue_entry_lookup as _build_queue_entry_lookup
from ..child.execution import find_queue_entry_by_id as _find_queue_entry_by_id
from ..engine.worker_execution import (
    EngineWorkerQueueDependencies,
    EngineWorkerTimingDependencies,
    build_engine_worker_process_default_factories,
    build_engine_worker_process_dependencies,
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
    terminate_process: Callable[..., bool]
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
    terminate_process: Callable[..., bool],
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


def queue_entry_by_id(
    queue_root: Path | str,
    queue_id: str,
    *,
    list_queue_fn: Callable[..., Any],
) -> Any | None:
    return _find_queue_entry_by_id(
        queue_root,
        queue_id,
        list_queue_fn=list_queue_fn,
    )


def build_queue_entry_lookup(
    *,
    list_queue_fn: Callable[[str | Path], Any],
) -> Callable[[str | Path, str], Any | None]:
    return _build_queue_entry_lookup(list_queue_fn=list_queue_fn)


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
    runner_dependencies_type: Callable[..., Any],
    terminate_process: Callable[..., bool],
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
        **build_engine_worker_process_default_factories(
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
    runner_dependencies_type: Callable[..., Any],
    cancel_check_interval_seconds: float,
) -> dict[str, DependencyFactory]:
    return build_worker_process_default_factories(
        config_factory=config_factory,
        admission_factory=admission_factory,
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
    runner_dependencies_type: Callable[..., Any],
    cancel_check_interval_seconds: float,
) -> dict[str, Any]:
    return {
        "timing": EngineWorkerTimingDependencies(now_utc_iso=callbacks.now_utc_iso),
        "queue": EngineWorkerQueueDependencies(
            get_cancel_requested=callbacks.get_cancel_requested,
            mark_completed=callbacks.mark_completed,
            mark_cancelled=callbacks.mark_cancelled,
            mark_failed=callbacks.mark_failed,
        ),
        "runner": build_engine_worker_process_dependencies(
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
    resolved: dict[str, Any] = {}
    for name, default_factory in default_factories.items():
        override = overrides.get(name)
        resolved[name] = default_factory() if override is None else override
    return container_builder(
        **resolved,
        execute_queue_entry_fn=execute_queue_entry_fn,
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
    "worker_process_dependency_callbacks_from_attrs",
]
