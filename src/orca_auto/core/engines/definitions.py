from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any


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
    build_report_markdown: Callable[..., list[str]]


@dataclass(frozen=True)
class EngineNotificationHooks:
    job_started: Callable[..., Any] | None = None
    job_finished: Callable[..., Any] | None = None
    retry: Callable[..., Any] | None = None


@dataclass(frozen=True)
class EngineDefinition:
    engine: str
    load_config: Callable[[str], Any]
    run_worker_child_job: Callable[..., int]
    queue_worker_module: str
    worker_pid_file_name: str
    build_worker_child_command: Callable[..., list[str]]
    runtime_roots_for_cfg: Callable[[Any], tuple[Path, ...]] | None = None
    queue_functions: EngineQueueFunctions | None = None
    runner_callbacks: EngineRunnerCallbacks | None = None
    context_builder: EngineContextBuilder | None = None
    artifact_adapter: EngineArtifactAdapter | None = None
    notification_hooks: EngineNotificationHooks | None = None
    queue_worker_runner: Callable[[list[str]], int] | None = None

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
        runner = (
            self.runner_callbacks.run_worker_child_job
            if self.runner_callbacks is not None
            else self.run_worker_child_job
        )
        return int(
            runner(
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
