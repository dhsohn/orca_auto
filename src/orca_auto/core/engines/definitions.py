from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .identity import own_engine_accept_entry

if TYPE_CHECKING:
    from orca_auto.core.queue.engine.runtime import EngineQueueRuntime


@dataclass(frozen=True)
class EngineQueueFunctions:
    runtime_roots_for_cfg: Callable[[Any], tuple[Path, ...]]
    list_queue: Callable[[str | Path], list[Any]]
    dequeue_next: Callable[[Path], Any | None]
    dequeue_entry_if_pending: Callable[..., Any | None] | None = None
    queue_entry_by_id: Callable[[str | Path, str], Any | None] | None = None
    worker_pid_file_name: str = ""


@dataclass(frozen=True)
class EngineRunnerCallbacks:
    run_worker_child_job: Callable[..., int]
    build_worker_child_command: Callable[..., list[str]]
    execute_queue_entry: Callable[..., Any] | None = None


@dataclass(frozen=True)
class EngineContextBuilder:
    build_worker_context: Callable[..., Any] | None = None
    build_queue_context: Callable[..., Any] | None = None


@dataclass(frozen=True)
class EngineArtifactAdapter:
    build_payload: Callable[..., dict[str, Any]]
    load_payload: Callable[..., dict[str, Any] | None]


@dataclass(frozen=True)
class EngineNotificationHooks:
    job_started: Callable[..., Any] | None = None
    job_finished: Callable[..., Any] | None = None
    retry: Callable[..., Any] | None = None


@dataclass(frozen=True)
class EngineDefinition:
    engine: str
    load_config: Callable[[str], Any]
    queue_worker_module: str
    queue_functions: EngineQueueFunctions
    runner_callbacks: EngineRunnerCallbacks
    context_builder: EngineContextBuilder | None = None
    artifact_adapter: EngineArtifactAdapter | None = None
    notification_hooks: EngineNotificationHooks | None = None
    queue_worker_runner: Callable[[list[str]], int] | None = None

    def build_queue_runtime(self) -> EngineQueueRuntime:
        """Build the canonical queue runtime declared by this definition."""
        from orca_auto.core.queue.engine.runtime import EngineQueueRuntime

        queue_functions = self.queue_functions
        worker_pid_file_name = queue_functions.worker_pid_file_name
        if not worker_pid_file_name:
            raise ValueError("worker_pid_file_name is required for queue runtime support")
        return EngineQueueRuntime(
            load_config=self.load_config,
            runtime_roots_for_cfg=queue_functions.runtime_roots_for_cfg,
            list_queue=queue_functions.list_queue,
            dequeue_next=queue_functions.dequeue_next,
            dequeue_entry_if_pending=queue_functions.dequeue_entry_if_pending,
            queue_entry_by_id_fn=queue_functions.queue_entry_by_id,
            worker_pid_file_name=worker_pid_file_name,
            accept_entry_fn=own_engine_accept_entry(self.engine),
        )

    def queue_worker_main(self, argv: list[str]) -> int:
        if self.queue_worker_runner is not None:
            return int(self.queue_worker_runner(argv))
        module = import_module(self.queue_worker_module)
        main = module.main
        return int(main(argv))

    def worker_child_main(
        self,
        *,
        config_path: str,
        queue_root: str | Path,
        queue_id: str,
        admission_token: str | None = None,
    ) -> int:
        return int(
            self.runner_callbacks.run_worker_child_job(
                config_path=config_path,
                queue_root=queue_root,
                queue_id=queue_id,
                admission_token=admission_token,
            )
        )


__all__ = [
    "EngineArtifactAdapter",
    "EngineContextBuilder",
    "EngineDefinition",
    "EngineNotificationHooks",
    "EngineQueueFunctions",
    "EngineRunnerCallbacks",
]
