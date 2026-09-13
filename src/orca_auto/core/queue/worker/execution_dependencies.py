from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from ..engine.worker_execution import (
    EngineWorkerProcessDependencyFactory,
    build_engine_worker_process_default_factories,
)

DependencyFactory = Callable[[], Any]
ProcessResultT = TypeVar("ProcessResultT")


@dataclass(frozen=True)
class WorkerConfigDependencies:
    load_config: Callable[..., Any]
    queue_entry_by_id: Callable[[Path | str, str], Any | None]


@dataclass(frozen=True)
class WorkerProcessDependencyCallbacks(Generic[ProcessResultT]):
    terminate_process: Callable[..., bool]
    wait_for_cancellable_process: Callable[..., ProcessResultT]
    sleep: Callable[..., Any]
    now_utc_iso: Callable[..., Any]
    get_cancel_requested: Callable[..., Any]
    mark_completed: Callable[..., Any]
    mark_cancelled: Callable[..., Any]
    mark_failed: Callable[..., Any]
    engine_runner_dependencies: Mapping[str, Any]


def build_worker_config_dependencies(
    *,
    load_config: Callable[..., Any],
    queue_entry_by_id_fn: Callable[[Path | str, str], Any | None],
) -> WorkerConfigDependencies:
    return WorkerConfigDependencies(
        load_config=load_config,
        queue_entry_by_id=queue_entry_by_id_fn,
    )


def build_worker_execution_dependencies_from_groups(
    dependencies_type: Callable[..., Any],
    groups: Mapping[str, Any],
) -> Any:
    resolved = {name: value for name, value in groups.items() if value is not None}
    return dependencies_type(**resolved)


def build_worker_process_default_factories(
    *,
    config_factory: DependencyFactory,
    runner_dependencies_type: EngineWorkerProcessDependencyFactory[ProcessResultT],
    terminate_process: Callable[..., bool],
    wait_for_cancellable_process: Callable[..., ProcessResultT],
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
    callbacks: WorkerProcessDependencyCallbacks[ProcessResultT],
    *,
    config_factory: DependencyFactory,
    runner_dependencies_type: EngineWorkerProcessDependencyFactory[ProcessResultT],
    cancel_check_interval_seconds: float,
) -> dict[str, DependencyFactory]:
    return build_worker_process_default_factories(
        config_factory=config_factory,
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


def build_worker_execution_dependency_container(
    container_builder: Callable[..., Any],
    overrides: Mapping[str, Any],
    default_factories: Mapping[str, DependencyFactory],
) -> Any:
    resolved: dict[str, Any] = {}
    for name, default_factory in default_factories.items():
        override = overrides.get(name)
        resolved[name] = default_factory() if override is None else override
    return container_builder(**resolved)


__all__ = [
    "WorkerConfigDependencies",
    "WorkerProcessDependencyCallbacks",
    "build_worker_config_dependencies",
    "build_worker_execution_dependencies_from_groups",
    "build_worker_execution_dependency_container",
    "build_worker_process_default_factories",
    "build_worker_process_default_factories_from_callbacks",
]
