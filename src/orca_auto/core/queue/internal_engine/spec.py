from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .child import InternalEngineWorkerChild
from .policies import InternalEngineAdmission, InternalEngineLifecycle
from .status import entry_status_is_running


@dataclass
class InternalEngineWorkerChildModuleFacade:
    worker_child: InternalEngineWorkerChild
    WORKER_JOB_MODULE: str
    WorkerShutdownRequested: type[BaseException]
    build_worker_child_command: Callable[..., list[str]]
    run_worker_child_job: Callable[..., int]
    shutdown_signal_handler_installer: Callable[..., Any]
    build_parser: Callable[..., Any]


@dataclass(frozen=True)
class InternalEngineSpec:
    engine: str
    worker_job_module: str = ""
    worker_pid_file_name: str = ""
    include_admission_root: bool = False

    def admission(self) -> InternalEngineAdmission:
        return InternalEngineAdmission(
            engine=self.engine,
            include_admission_root=self.include_admission_root,
        )

    def lifecycle(self) -> InternalEngineLifecycle:
        return InternalEngineLifecycle(engine=self.engine)

    def worker_child(
        self,
        shutdown_exception_type: type[BaseException],
        *,
        entry_ready_fn: Callable[[Any], bool] = entry_status_is_running,
        process_dequeued_entry_kwargs_fn: Callable[[], Mapping[str, Any]] | None = None,
        outcome_exit_code_fn: Callable[[Any], int] | None = None,
    ) -> InternalEngineWorkerChild:
        if not self.worker_job_module:
            raise ValueError("worker_job_module is required for worker child support")
        return InternalEngineWorkerChild(
            worker_job_module=self.worker_job_module,
            include_admission_root=self.include_admission_root,
            shutdown_exception_type=shutdown_exception_type,
            entry_ready_fn=entry_ready_fn,
            process_dequeued_entry_kwargs_fn=process_dequeued_entry_kwargs_fn,
            outcome_exit_code_fn=outcome_exit_code_fn,
        )

    def worker_child_module_facade(
        self,
        shutdown_exception_type: type[BaseException],
        *,
        entry_ready_fn: Callable[[Any], bool] = entry_status_is_running,
        process_dequeued_entry_kwargs_fn: Callable[[], Mapping[str, Any]] | None = None,
        outcome_exit_code_fn: Callable[[Any], int] | None = None,
        build_worker_child_command: Callable[..., list[str]] | None = None,
    ) -> InternalEngineWorkerChildModuleFacade:
        worker_child = self.worker_child(
            shutdown_exception_type,
            entry_ready_fn=entry_ready_fn,
            process_dequeued_entry_kwargs_fn=process_dequeued_entry_kwargs_fn,
            outcome_exit_code_fn=outcome_exit_code_fn,
        )
        return InternalEngineWorkerChildModuleFacade(
            worker_child=worker_child,
            WORKER_JOB_MODULE=self.worker_job_module,
            WorkerShutdownRequested=shutdown_exception_type,
            build_worker_child_command=build_worker_child_command
            or worker_child.build_worker_child_command,
            run_worker_child_job=worker_child.run_worker_child_job,
            shutdown_signal_handler_installer=worker_child.shutdown_signal_handler_installer,
            build_parser=worker_child.build_parser,
        )


__all__ = [
    "InternalEngineSpec",
    "InternalEngineWorkerChildModuleFacade",
]
