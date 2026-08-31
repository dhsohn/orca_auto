from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orca_auto.core.queue.worker.execution_dependencies import (
    WorkerProcessDependencyCallbacks,
    build_worker_process_dependency_groups,
    worker_process_dependency_callbacks_from_attrs,
)
from orca_auto.flow.engines.crest import execution as _worker_execution


@dataclass(frozen=True)
class CrestQueueRuntimeWorkerExecutionCallbacks:
    terminate_process: Callable[..., bool]
    wait_for_cancellable_process: Callable[..., _worker_execution.CrestRunResult]
    sleep: Callable[..., Any]
    now_utc_iso: Callable[..., Any]
    get_cancel_requested: Callable[..., Any]
    mark_completed: Callable[..., Any]
    mark_cancelled: Callable[..., Any]
    mark_failed: Callable[..., Any]
    start_crest_job: Callable[..., Any]
    finalize_crest_job: Callable[..., _worker_execution.CrestRunResult]
    write_running_state: Callable[..., Any]
    write_execution_artifacts: Callable[..., Any]
    upsert_job_record: Callable[..., Any]
    notify_job_started: Callable[..., Any]
    notify_job_finished: Callable[..., Any]

    @property
    def process_callbacks(
        self,
    ) -> WorkerProcessDependencyCallbacks[_worker_execution.CrestRunResult]:
        return worker_process_dependency_callbacks_from_attrs(
            self,
            engine_runner_dependency_names=("start_crest_job", "finalize_crest_job"),
        )


def build_queue_runtime_worker_execution_dependencies(
    callbacks: CrestQueueRuntimeWorkerExecutionCallbacks,
    *,
    cancel_check_interval_seconds: int,
) -> _worker_execution.WorkerExecutionDependencies:
    process_groups = build_worker_process_dependency_groups(
        callbacks.process_callbacks,
        runner_dependencies_type=_worker_execution.WorkerRunnerDependencies,
        cancel_check_interval_seconds=cancel_check_interval_seconds,
    )
    return _worker_execution.build_worker_execution_dependencies(
        **process_groups,
        artifacts=_worker_execution.WorkerArtifactDependencies(
            write_running_state=callbacks.write_running_state,
            write_execution_artifacts=callbacks.write_execution_artifacts,
        ),
        tracking=_worker_execution.WorkerTrackingDependencies(
            upsert_job_record=callbacks.upsert_job_record,
            notify_job_started=callbacks.notify_job_started,
            notify_job_finished=callbacks.notify_job_finished,
        ),
    )


__all__ = [
    "CrestQueueRuntimeWorkerExecutionCallbacks",
    "build_queue_runtime_worker_execution_dependencies",
]
