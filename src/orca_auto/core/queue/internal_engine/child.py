from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..child import execution as _child_execution
from ..engine import child as _engine_child
from .status import entry_status_is_running


class _WorkerShutdownRequestedBase(RuntimeError):
    def __init__(self, context: Any):
        super().__init__("worker_shutdown")
        self.context = context


def create_worker_shutdown_exception_type(
    module_name: str,
    class_name: str = "WorkerShutdownRequested",
) -> type[_WorkerShutdownRequestedBase]:
    return type(
        class_name,
        (_WorkerShutdownRequestedBase,),
        {"__module__": module_name, "__qualname__": class_name},
    )


@dataclass(frozen=True)
class InternalEngineWorkerChild:
    worker_job_module: str
    include_admission_root: bool
    shutdown_exception_type: type[BaseException]
    entry_ready_fn: Callable[[Any], bool] = entry_status_is_running
    process_dequeued_entry_kwargs_fn: Callable[[], Mapping[str, Any]] | None = None
    outcome_exit_code_fn: Callable[[Any], int] | None = None

    @property
    def command_spec(self) -> _engine_child.WorkerChildCommandSpec:
        return _engine_child.WorkerChildCommandSpec(
            worker_job_module=self.worker_job_module,
            include_admission_root=self.include_admission_root,
        )

    @property
    def run_spec(self) -> _engine_child.WorkerChildRunSpec:
        return _engine_child.WorkerChildRunSpec(
            shutdown_exception_type=self.shutdown_exception_type,
            entry_ready_fn=self.entry_ready_fn,
            outcome_exit_code_fn=self.outcome_exit_code_fn,
        )

    def build_worker_child_command(
        self,
        *,
        config_path: str,
        queue_root: str | Path,
        queue_id: str,
        admission_root: str | Path | None = None,
        admission_token: str | None = None,
    ) -> list[str]:
        return _engine_child.build_engine_worker_child_command(
            spec=self.command_spec,
            config_path=config_path,
            queue_root=queue_root,
            queue_id=queue_id,
            admission_root=admission_root,
            admission_token=admission_token,
        )

    def install_shutdown_signal_handlers(
        self,
        controller: _child_execution.ChildWorkerShutdownController,
        *,
        install_signal_handlers_fn: Callable[[Callable[[], None]], Any],
    ) -> None:
        _child_execution.install_shutdown_request_handlers(
            controller,
            install_signal_handlers_fn=install_signal_handlers_fn,
        )

    def shutdown_signal_handler_installer(
        self,
        install_signal_handlers_fn: Callable[[Callable[[], None]], Any],
    ) -> Callable[[_child_execution.ChildWorkerShutdownController], None]:
        return lambda controller: self.install_shutdown_signal_handlers(
            controller,
            install_signal_handlers_fn=install_signal_handlers_fn,
        )

    def run_worker_child_job(
        self,
        *,
        config_path: str,
        queue_root: str | Path,
        queue_id: str,
        admission_token: str | None = None,
        load_config_fn: Callable[[str], Any],
        find_queue_entry_fn: Callable[[Path, str], Any | None],
        admission_root_fn: Callable[[Any], str | Path],
        release_slot_fn: Callable[[str | Path, str], Any],
        install_signal_handlers_fn: Callable[
            [_child_execution.ChildWorkerShutdownController],
            Any,
        ],
        process_dequeued_entry_fn: Callable[..., Any],
        dependencies_fn: Callable[[], Any],
        requeue_running_entry_fn: Callable[[Path, str], Any],
        mark_recovery_pending_context_fn: Callable[..., Any],
        process_dequeued_entry_kwargs: Mapping[str, Any] | None = None,
        await_parent_admission_handoff_fn: Callable[[Any, str], bool] | None = None,
        **extra_process_dequeued_entry_kwargs: Any,
    ) -> int:
        active_process_kwargs = self.process_dequeued_entry_kwargs(
            process_dequeued_entry_kwargs,
            extra_process_dequeued_entry_kwargs,
        )
        child_kwargs: dict[str, Any] = {}
        if await_parent_admission_handoff_fn is not None:
            child_kwargs["await_parent_admission_handoff_fn"] = await_parent_admission_handoff_fn
        return _engine_child.run_engine_worker_child_job(
            spec=self.run_spec,
            config_path=config_path,
            queue_root=queue_root,
            queue_id=queue_id,
            load_config_fn=load_config_fn,
            find_queue_entry_fn=find_queue_entry_fn,
            admission_root_fn=admission_root_fn,
            release_slot_fn=release_slot_fn,
            admission_token=admission_token,
            install_signal_handlers_fn=install_signal_handlers_fn,
            process_dequeued_entry_fn=process_dequeued_entry_fn,
            dependencies_fn=dependencies_fn,
            requeue_running_entry_fn=requeue_running_entry_fn,
            mark_recovery_pending_context_fn=mark_recovery_pending_context_fn,
            process_dequeued_entry_kwargs=active_process_kwargs,
            **child_kwargs,
        )

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog=f"python -m {self.worker_job_module}")
        parser.add_argument("--config", required=True)
        parser.add_argument("--queue-root", required=True)
        parser.add_argument("--queue-id", required=True)
        parser.add_argument("--admission-token", default=None)
        return parser

    def process_dequeued_entry_kwargs(
        self,
        explicit_kwargs: Mapping[str, Any] | None = None,
        extra_kwargs: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any] | None:
        merged: dict[str, Any] = {}
        if self.process_dequeued_entry_kwargs_fn is not None:
            merged.update(self.process_dequeued_entry_kwargs_fn())
        if explicit_kwargs:
            merged.update(explicit_kwargs)
        if extra_kwargs:
            merged.update(extra_kwargs)
        return merged or None


__all__ = [
    "InternalEngineWorkerChild",
    "create_worker_shutdown_exception_type",
]
