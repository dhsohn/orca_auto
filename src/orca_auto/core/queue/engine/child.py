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
from orca_auto.core.statuses import STATUS_CANCELLED, STATUS_COMPLETED

from ..child import entrypoint as _child_entrypoint
from ..child.entrypoint import ChildWorkerEntrypointJob
from ..child.execution import ChildWorkerShutdownController
from ..child.process import requeue_result_is_cancelled
from ..worker import build_background_worker_command


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
class WorkerChildCommandSpec:
    worker_job_module: str
    include_admission_root: bool = True


def _shutdown_exception_context(exc: BaseException) -> Any:
    context_attr = "context"
    return getattr(exc, context_attr)


@dataclass(frozen=True)
class WorkerChildRunSpec:
    shutdown_exception_type: type[BaseException]
    entry_ready_fn: Callable[[Any], bool] | None = None
    shutdown_context_fn: Callable[[BaseException], Any] = _shutdown_exception_context
    outcome_exit_code_fn: Callable[[Any], int] | None = None


@dataclass(frozen=True)
class _EngineChildJobRunner:
    spec: WorkerChildRunSpec
    queue_id: str
    controller: ChildWorkerShutdownController
    process_dequeued_entry_fn: Callable[..., Any]
    dependencies_fn: Callable[[], Any]
    requeue_running_entry_fn: Callable[[Path, str], Any]
    mark_recovery_pending_context_fn: Callable[..., Any]
    process_kwargs: Mapping[str, Any]
    admission_token: str | None

    def run(self, job: ChildWorkerEntrypointJob) -> int:
        try:
            outcome = self._process(job)
        except self.spec.shutdown_exception_type as exc:
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

    def _handle_shutdown(self, job: ChildWorkerEntrypointJob, exc: BaseException) -> int:
        updated = self.requeue_running_entry_fn(job.queue_root, self.queue_id)
        if not requeue_result_is_cancelled(updated):
            self.mark_recovery_pending_context_fn(
                job.cfg,
                self.spec.shutdown_context_fn(exc),
                reason="worker_shutdown",
            )
        return 0


def build_engine_worker_child_command(
    *,
    spec: WorkerChildCommandSpec,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    admission_root: str | Path | None = None,
    admission_token: str | None = None,
) -> list[str]:
    return build_background_worker_command(
        config_path=config_path,
        queue_root=Path(queue_root),
        queue_id=queue_id,
        worker_job_module=spec.worker_job_module,
        admission_root=admission_root,
        admission_token=admission_token,
        include_admission_root=spec.include_admission_root,
    )


def activate_child_worker_admission(
    job: ChildWorkerEntrypointJob,
    admission_token: str | None,
    *,
    work_dir: str | Path,
    queue_id: str,
    source: str,
    activate_reserved_slot_fn: Callable[..., Any],
) -> bool:
    return _child_entrypoint.activate_child_worker_admission(
        job,
        admission_token,
        work_dir=work_dir,
        queue_id=queue_id,
        source=source,
        activate_reserved_slot_fn=activate_reserved_slot_fn,
    )


def load_engine_child_job(
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    load_config_fn: Callable[[str], Any],
    find_queue_entry_fn: Callable[[Path, str], Any | None],
    admission_root_fn: Callable[[Any], str | Path],
    release_slot_fn: Callable[[str | Path, str], Any],
    admission_token: str | None = None,
    entry_ready_fn: Callable[[Any], bool] | None = None,
) -> ChildWorkerEntrypointJob | None:
    return _child_entrypoint.load_child_worker_entrypoint_job(
        config_path=config_path,
        queue_root=queue_root,
        queue_id=queue_id,
        load_config_fn=load_config_fn,
        find_queue_entry_fn=find_queue_entry_fn,
        entry_ready_fn=entry_ready_fn,
        admission_token=admission_token,
        admission_root_fn=admission_root_fn,
        release_slot_fn=release_slot_fn,
    )


def run_child_job_with_admission_scope(
    job: ChildWorkerEntrypointJob,
    admission_token: str | None,
    *,
    release_slot_fn: Callable[[str | Path, str], Any],
    run_job_fn: Callable[[ChildWorkerEntrypointJob], int],
) -> int:
    with _child_entrypoint.child_worker_admission_scope(
        job,
        admission_token,
        release_slot_fn=release_slot_fn,
    ):
        return run_job_fn(job)


def run_loaded_engine_child_job(
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    load_config_fn: Callable[[str], Any],
    find_queue_entry_fn: Callable[[Path, str], Any | None],
    admission_root_fn: Callable[[Any], str | Path],
    release_slot_fn: Callable[[str | Path, str], Any],
    run_job_fn: Callable[[ChildWorkerEntrypointJob], int],
    admission_token: str | None = None,
    entry_ready_fn: Callable[[Any], bool] | None = None,
    prepare_job_fn: Callable[[ChildWorkerEntrypointJob], bool] | None = None,
    missing_exit_code: int = 1,
    prepare_failed_exit_code: int = 1,
) -> int:
    job = load_engine_child_job(
        config_path=config_path,
        queue_root=queue_root,
        queue_id=queue_id,
        load_config_fn=load_config_fn,
        find_queue_entry_fn=find_queue_entry_fn,
        entry_ready_fn=entry_ready_fn,
        admission_token=admission_token,
        admission_root_fn=admission_root_fn,
        release_slot_fn=release_slot_fn,
    )
    if job is None:
        return missing_exit_code
    if prepare_job_fn is not None and not prepare_job_fn(job):
        return prepare_failed_exit_code
    return run_child_job_with_admission_scope(
        job,
        admission_token,
        release_slot_fn=release_slot_fn,
        run_job_fn=run_job_fn,
    )


def run_engine_worker_child_job(
    *,
    spec: WorkerChildRunSpec,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    load_config_fn: Callable[[str], Any],
    find_queue_entry_fn: Callable[[Path, str], Any | None],
    admission_root_fn: Callable[[Any], str | Path],
    release_slot_fn: Callable[[str | Path, str], Any],
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

    def prepare_loaded_job(_job: ChildWorkerEntrypointJob) -> bool:
        if admission_token and not await_parent_admission_handoff_fn(_job, admission_token):
            return False
        install_signal_handlers_fn(controller)
        return True

    return run_loaded_engine_child_job(
        config_path=config_path,
        queue_root=queue_root,
        queue_id=queue_id,
        load_config_fn=load_config_fn,
        find_queue_entry_fn=find_queue_entry_fn,
        entry_ready_fn=spec.entry_ready_fn,
        admission_root_fn=admission_root_fn,
        release_slot_fn=release_slot_fn,
        admission_token=admission_token,
        prepare_job_fn=prepare_loaded_job,
        run_job_fn=runner.run,
    )


def outcome_exit_code(
    outcome: Any,
    *,
    success_statuses: set[str] | frozenset[str] = frozenset({STATUS_COMPLETED, STATUS_CANCELLED}),
) -> int:
    status = str(getattr(outcome.result, "status", "")).strip().lower()
    return 0 if status in success_statuses else 1


__all__ = [
    "WorkerChildCommandSpec",
    "WorkerChildRunSpec",
    "activate_child_worker_admission",
    "await_parent_admission_handoff",
    "build_engine_worker_child_command",
    "load_engine_child_job",
    "outcome_exit_code",
    "run_child_job_with_admission_scope",
    "run_engine_worker_child_job",
    "run_loaded_engine_child_job",
]
