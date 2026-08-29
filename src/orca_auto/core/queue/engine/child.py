from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.admission import (
    build_slot_engine_process_preparer,
    build_slot_engine_process_registrar,
    get_slot,
)

from ..child import execution as _child_execution
from ..child.execution import ChildWorkerEntrypointJob, ChildWorkerShutdownController
from ..child.process import requeue_result_is_cancelled
from .worker_execution import WorkerShutdownRequested


def await_parent_admission_handoff(
    job: ChildWorkerEntrypointJob,
    admission_token: str,
    *,
    timeout_seconds: float = 10.0,
    monotonic_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait until the parent atomically attaches this child PID to the slot."""
    deadline = monotonic_fn() + max(0.0, float(timeout_seconds))
    while True:
        slot = get_slot(job.admission_root(), admission_token)
        if slot is None:
            return False
        if slot.owner_pid == os.getpid():
            return True
        if monotonic_fn() >= deadline:
            return False
        sleep_fn(0.01)


@dataclass(frozen=True)
class WorkerChildRunSpec:
    entry_ready_fn: Callable[[Any], bool] | None = None
    outcome_exit_code_fn: Callable[[Any], int] | None = None


@dataclass(frozen=True)
class _EngineChildJobRunner:
    spec: WorkerChildRunSpec
    queue_id: str
    controller: ChildWorkerShutdownController
    process_dequeued_entry_fn: Callable[..., Any]
    dependencies_fn: Callable[[], Any]
    requeue_running_entry_fn: Callable[..., Any]
    mark_recovery_pending_context_fn: Callable[..., Any]
    process_kwargs: Mapping[str, Any]
    admission_token: str | None

    def run(self, job: ChildWorkerEntrypointJob) -> int:
        try:
            outcome = self._process(job)
        except WorkerShutdownRequested as exc:
            return self._handle_shutdown(job, exc)
        return self._exit_code(outcome)

    def _process(self, job: ChildWorkerEntrypointJob) -> Any:
        process_kwargs = dict(self.process_kwargs)
        if self.admission_token:
            durable_preparer = build_slot_engine_process_preparer(
                job.admission_root(),
                self.admission_token,
            )
            durable_registrar = build_slot_engine_process_registrar(
                job.admission_root(),
                self.admission_token,
            )
            extra_registrar = process_kwargs.get("register_running_job")
            extra_preparer = process_kwargs.get("prepare_running_job")

            def prepare_running_job() -> None:
                durable_preparer()
                if callable(extra_preparer):
                    extra_preparer()

            def register_running_job(running: Any | None) -> None:
                durable_registrar(running)
                if callable(extra_registrar):
                    extra_registrar(running)

            process_kwargs["register_running_job"] = register_running_job
            process_kwargs["prepare_running_job"] = prepare_running_job
        return self.process_dequeued_entry_fn(
            job.cfg,
            job.entry,
            queue_root=job.queue_root,
            **process_kwargs,
            dependencies=self.dependencies_fn(),
            shutdown_requested=self.controller.is_requested,
        )

    def _exit_code(self, outcome: Any) -> int:
        if self.spec.outcome_exit_code_fn is None:
            return 0
        return int(self.spec.outcome_exit_code_fn(outcome))

    def _handle_shutdown(self, job: ChildWorkerEntrypointJob, exc: WorkerShutdownRequested) -> int:
        task_id = str(getattr(job.entry, "task_id", "") or "").strip()
        generation_kwargs = {"expected_entry": job.entry}
        if task_id:
            generation_kwargs["expected_task_id"] = task_id
        updated = self.requeue_running_entry_fn(
            job.queue_root,
            self.queue_id,
            **generation_kwargs,
        )
        if updated is not None and not requeue_result_is_cancelled(updated):
            self.mark_recovery_pending_context_fn(
                job.cfg,
                exc.context,
                reason="worker_shutdown",
            )
        return 0


def run_engine_worker_child_job(
    *,
    spec: WorkerChildRunSpec,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    load_config_fn: Callable[[str], Any],
    find_queue_entry_fn: Callable[[Path, str], Any | None],
    admission_root_fn: Callable[[Any], str | Path],
    install_signal_handlers_fn: Callable[[ChildWorkerShutdownController], Any],
    process_dequeued_entry_fn: Callable[..., Any],
    dependencies_fn: Callable[[], Any],
    requeue_running_entry_fn: Callable[[Path, str], Any],
    mark_recovery_pending_context_fn: Callable[..., Any],
    admission_token: str | None = None,
    process_dequeued_entry_kwargs: Mapping[str, Any] | None = None,
    await_parent_admission_handoff_fn: Callable[[ChildWorkerEntrypointJob, str], bool] = (
        await_parent_admission_handoff
    ),
) -> int:
    controller = ChildWorkerShutdownController()
    runner = _EngineChildJobRunner(
        spec=spec,
        queue_id=queue_id,
        controller=controller,
        process_dequeued_entry_fn=process_dequeued_entry_fn,
        dependencies_fn=dependencies_fn,
        requeue_running_entry_fn=requeue_running_entry_fn,
        mark_recovery_pending_context_fn=mark_recovery_pending_context_fn,
        process_kwargs=dict(process_dequeued_entry_kwargs or {}),
        admission_token=admission_token,
    )
    job = _child_execution.load_child_worker_entrypoint_job(
        config_path=config_path,
        queue_root=queue_root,
        queue_id=queue_id,
        load_config_fn=load_config_fn,
        find_queue_entry_fn=find_queue_entry_fn,
        admission_root_fn=admission_root_fn,
        entry_ready_fn=spec.entry_ready_fn,
    )
    if job is None:
        return 1
    if admission_token and not await_parent_admission_handoff_fn(job, admission_token):
        return 1
    install_signal_handlers_fn(controller)
    # The child never releases the admission slot itself, even when it is torn
    # down by an asynchronous BaseException between Popen and the publication
    # of the active identity: the parent owns final release, and dropping the
    # only capacity fence here would break the parent recovery path.
    return runner.run(job)


__all__ = [
    "WorkerChildRunSpec",
    "await_parent_admission_handoff",
    "run_engine_worker_child_job",
]
