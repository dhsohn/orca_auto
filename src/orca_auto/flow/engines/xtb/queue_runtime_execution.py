from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orca_auto.core.queue.worker.execution_dependencies import (
    WorkerProcessDependencyCallbacks,
    build_worker_process_dependency_groups,
    worker_process_dependency_callbacks_from_attrs,
)
from orca_auto.flow.engines.xtb import execution as _worker_execution


@dataclass(frozen=True)
class XtbQueueRuntimeWorkerExecutionCallbacks:
    activate_reserved_slot: Callable[..., Any]
    release_slot: Callable[..., Any]
    load_config: Callable[..., Any]
    queue_entry_by_id: Callable[..., Any]
    job_dir: Callable[..., Any]
    selected_xyz: Callable[..., Any]
    job_type: Callable[..., Any]
    reaction_key: Callable[..., Any]
    input_summary: Callable[..., Any]
    entry_resource_request: Callable[..., Any]
    matching_state: Callable[..., Any]
    is_recovery_pending: Callable[..., Any]
    write_running_state: Callable[..., Any]
    build_terminal_result: Callable[..., Any]
    finalize_execution_result: Callable[..., Any]
    upsert_job_record: Callable[..., Any]
    notify_job_started: Callable[..., Any]
    execute_queue_entry: Callable[..., Any]
    run_xtb_ranking_job: Callable[..., Any]
    start_xtb_job: Callable[..., Any]
    finalize_xtb_job: Callable[..., Any]
    run_path_search_ts_hessian_followup: Callable[..., Any]
    terminate_process: Callable[..., Any]
    wait_for_cancellable_process: Callable[..., Any]
    sleep: Callable[..., Any]
    now_utc_iso: Callable[..., Any]
    get_cancel_requested: Callable[..., Any]
    mark_completed: Callable[..., Any]
    mark_cancelled: Callable[..., Any]
    mark_failed: Callable[..., Any]

    @property
    def process_callbacks(self) -> WorkerProcessDependencyCallbacks:
        return worker_process_dependency_callbacks_from_attrs(
            self,
            engine_runner_dependency_names=(
                "run_xtb_ranking_job",
                "start_xtb_job",
                "finalize_xtb_job",
                "run_path_search_ts_hessian_followup",
            ),
        )


def build_queue_runtime_worker_execution_dependencies(
    callbacks: XtbQueueRuntimeWorkerExecutionCallbacks,
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
        config=_worker_execution.WorkerConfigDependencies(
            load_config=callbacks.load_config,
            queue_entry_by_id=callbacks.queue_entry_by_id,
        ),
        admission=_worker_execution.WorkerAdmissionDependencies(
            activate_reserved_slot=callbacks.activate_reserved_slot,
            release_slot=callbacks.release_slot,
        ),
        context=_worker_execution.WorkerContextDependencies(
            job_dir=callbacks.job_dir,
            selected_xyz=callbacks.selected_xyz,
            job_type=callbacks.job_type,
            reaction_key=callbacks.reaction_key,
            input_summary=callbacks.input_summary,
            entry_resource_request=callbacks.entry_resource_request,
            matching_state=callbacks.matching_state,
            is_recovery_pending=callbacks.is_recovery_pending,
        ),
        artifacts=_worker_execution.WorkerArtifactDependencies(
            write_running_state=callbacks.write_running_state,
            build_terminal_result=callbacks.build_terminal_result,
            finalize_execution_result=callbacks.finalize_execution_result,
        ),
        tracking=_worker_execution.WorkerTrackingDependencies(
            upsert_job_record=callbacks.upsert_job_record,
            notify_job_started=callbacks.notify_job_started,
        ),
        execute_queue_entry_fn=callbacks.execute_queue_entry,
    )


__all__ = [
    "XtbQueueRuntimeWorkerExecutionCallbacks",
    "build_queue_runtime_worker_execution_dependencies",
]
