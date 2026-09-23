from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import cast

from orca_auto.core.indexing.roots import runtime_roots_for_cfg as _runtime_roots_for_cfg
from orca_auto.core.queue.dependencies import ConfigT, QueueEntryDequeuer, WorkerConfig
from orca_auto.core.queue.types import QueueEntry

from .definitions import (
    EngineDefinition,
    EngineQueueFunctions,
    EngineRunnerCallbacks,
    WorkerChildCommandBuilder,
    WorkerChildRunner,
)

QueueEntryById = Callable[[str | Path, str], QueueEntry | None]
QueueList = Callable[[str | Path], list[QueueEntry]]
QueueDequeuer = Callable[[Path], QueueEntry | None]
RuntimeRootsBuilder = Callable[[ConfigT], tuple[Path, ...]]
QueueWorkerRunner = Callable[[list[str]], int]


def build_lazy_worker_child_runner(
    module_name: str,
    function_name: str,
) -> WorkerChildRunner:

    def run_worker_child_job(
        *,
        config_path: str,
        queue_root: str | Path,
        queue_id: str,
        admission_token: str | None = None,
    ) -> int:
        module = import_module(module_name)
        run_worker_child = cast(WorkerChildRunner, getattr(module, function_name))
        return int(
            run_worker_child(
                config_path=config_path,
                queue_root=queue_root,
                queue_id=queue_id,
                admission_token=admission_token,
            )
        )

    return run_worker_child_job


def build_lazy_queue_worker_runner(module_name: str) -> QueueWorkerRunner:

    def run_queue_worker(argv: list[str]) -> int:
        module = import_module(module_name)
        queue_worker_main = cast(QueueWorkerRunner, module.main)
        return int(queue_worker_main(argv))

    return run_queue_worker


def build_worker_child_command_for_engine(engine: str) -> WorkerChildCommandBuilder:
    from .worker_child import build_worker_child_command_for_engine as build_command

    return build_command(engine)


def build_queue_entry_by_id(list_queue: QueueList) -> QueueEntryById:
    def lookup(root: str | Path, queue_id: str) -> QueueEntry | None:
        return next((entry for entry in list_queue(Path(root)) if entry.queue_id == queue_id), None)

    return lookup


def build_engine_runtime_roots(engine: str) -> RuntimeRootsBuilder[WorkerConfig]:
    engine_id = str(engine).strip().lower()

    def runtime_roots(cfg: WorkerConfig) -> tuple[Path, ...]:
        return _runtime_roots_for_cfg(cfg, engine=engine_id)

    return runtime_roots


def _default_list_queue(root: str | Path) -> list[QueueEntry]:
    from orca_auto.core.queue import list_queue

    return list_queue(root)


def _default_dequeue_next(root: Path) -> QueueEntry | None:
    from orca_auto.core.queue import dequeue_next

    return dequeue_next(root)


def _default_dequeue_entry_if_pending(
    root: Path,
    queue_id: str,
    *,
    expected_entry: QueueEntry | None = None,
) -> QueueEntry | None:
    from orca_auto.core.queue import dequeue_entry_if_pending

    return dequeue_entry_if_pending(root, queue_id, expected_entry=expected_entry)


def build_queue_engine_definition(
    *,
    engine: str,
    load_config: Callable[[str], ConfigT],
    run_worker_child_job: WorkerChildRunner,
    queue_worker_runner: QueueWorkerRunner,
    worker_pid_file_name: str,
    list_queue: QueueList | None = None,
    dequeue_next: QueueDequeuer | None = None,
    dequeue_entry_if_pending: QueueEntryDequeuer[QueueEntry] | None = None,
    build_worker_child_command: WorkerChildCommandBuilder | None = None,
    runtime_roots_for_cfg: RuntimeRootsBuilder[ConfigT] | None = None,
    queue_entry_by_id: QueueEntryById | None = None,
) -> EngineDefinition[ConfigT]:
    engine_id = str(engine).strip().lower()
    runtime_roots = runtime_roots_for_cfg or build_engine_runtime_roots(engine)
    queue_lister = list_queue or _default_list_queue
    queue_dequeuer = dequeue_next or _default_dequeue_next
    queue_entry_dequeuer = dequeue_entry_if_pending or _default_dequeue_entry_if_pending
    worker_child_command = build_worker_child_command or build_worker_child_command_for_engine(
        engine_id
    )
    return EngineDefinition(
        engine=engine_id,
        load_config=load_config,
        queue_functions=EngineQueueFunctions(
            runtime_roots_for_cfg=runtime_roots,
            list_queue=queue_lister,
            dequeue_next=queue_dequeuer,
            dequeue_entry_if_pending=queue_entry_dequeuer,
            queue_entry_by_id=queue_entry_by_id or build_queue_entry_by_id(queue_lister),
            worker_pid_file_name=worker_pid_file_name,
        ),
        runner_callbacks=EngineRunnerCallbacks(
            run_worker_child_job=run_worker_child_job,
            build_worker_child_command=worker_child_command,
        ),
        queue_worker_runner=queue_worker_runner,
    )


__all__ = [
    "build_engine_runtime_roots",
    "build_lazy_queue_worker_runner",
    "build_lazy_worker_child_runner",
    "build_queue_engine_definition",
    "build_queue_entry_by_id",
    "build_worker_child_command_for_engine",
]
