from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Generic, Protocol

from orca_auto.core.queue.dependencies import ConfigT, QueueEntryDequeuer
from orca_auto.core.queue.types import QueueEntry

from .identity import own_engine_accept_entry

if TYPE_CHECKING:
    from orca_auto.core.queue.engine.runtime import EngineQueueRuntime


class WorkerChildRunner(Protocol):
    def __call__(
        self,
        *,
        config_path: str,
        queue_root: str | Path,
        queue_id: str,
        admission_token: str | None = None,
    ) -> int: ...


class WorkerChildCommandBuilder(Protocol):
    def __call__(
        self,
        *,
        config_path: str,
        queue_root: str | Path,
        queue_id: str,
        admission_token: str | None = None,
    ) -> list[str]: ...


@dataclass(frozen=True)
class EngineQueueFunctions(Generic[ConfigT]):
    runtime_roots_for_cfg: Callable[[ConfigT], tuple[Path, ...]]
    list_queue: Callable[[str | Path], list[QueueEntry]]
    dequeue_next: Callable[[Path], QueueEntry | None]
    dequeue_entry_if_pending: QueueEntryDequeuer[QueueEntry] | None = None
    queue_entry_by_id: Callable[[str | Path, str], QueueEntry | None] | None = None
    worker_pid_file_name: str = ""


@dataclass(frozen=True)
class EngineRunnerCallbacks:
    run_worker_child_job: WorkerChildRunner
    build_worker_child_command: WorkerChildCommandBuilder


@dataclass(frozen=True)
class EngineDefinition(Generic[ConfigT]):
    engine: str
    load_config: Callable[[str], ConfigT]
    queue_functions: EngineQueueFunctions[ConfigT]
    runner_callbacks: EngineRunnerCallbacks
    queue_worker_runner: Callable[[list[str]], int]

    def build_queue_runtime(self) -> EngineQueueRuntime[ConfigT]:
        """Build the canonical queue runtime declared by this definition."""
        from orca_auto.core.queue.engine.runtime import EngineQueueRuntime

        queue_functions = self.queue_functions
        worker_pid_file_name = queue_functions.worker_pid_file_name
        if not worker_pid_file_name:
            raise ValueError("worker_pid_file_name is required for queue runtime support")
        return EngineQueueRuntime(
            runtime_roots_for_cfg=queue_functions.runtime_roots_for_cfg,
            list_queue=queue_functions.list_queue,
            dequeue_next=queue_functions.dequeue_next,
            dequeue_entry_if_pending=queue_functions.dequeue_entry_if_pending,
            queue_entry_by_id_fn=queue_functions.queue_entry_by_id,
            worker_pid_file_name=worker_pid_file_name,
            accept_entry_fn=own_engine_accept_entry(self.engine),
        )

    def queue_worker_main(self, argv: list[str]) -> int:
        return int(self.queue_worker_runner(argv))

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
    "EngineDefinition",
    "EngineQueueFunctions",
    "EngineRunnerCallbacks",
]
