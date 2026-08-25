from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any

from orca_auto.core.indexing.roots import runtime_roots_for_cfg as _runtime_roots_for_cfg
from orca_auto.core.queue.child.execution import build_queue_entry_lookup

from .definitions import (
    EngineDefinition,
    EngineQueueFunctions,
    EngineRunnerCallbacks,
)

QueueEntryById = Callable[[str | Path, str], Any | None]
QueueList = Callable[[str | Path], list[Any]]
QueueDequeuer = Callable[[Path], Any | None]
QueueEntryDequeuer = Callable[[Path, str], Any | None]
RuntimeRootsBuilder = Callable[[Any], tuple[Path, ...]]
WorkerChildCommandBuilder = Callable[..., list[str]]
WorkerChildRunner = Callable[..., int]
QueueWorkerRunner = Callable[[list[str]], int]


def _lazy_callable(module_name: str, function_name: str) -> Callable[..., Any]:
    def call(*args: Any, **kwargs: Any) -> Any:
        module = import_module(module_name)
        function = getattr(module, function_name)
        return function(*args, **kwargs)

    return call


def build_lazy_worker_child_runner(
    module_name: str,
    function_name: str,
) -> WorkerChildRunner:
    run_worker_child = _lazy_callable(module_name, function_name)

    def run_worker_child_job(
        *,
        config_path: str,
        queue_root: str | Path,
        queue_id: str,
        admission_token: str | None = None,
        **_unused: Any,
    ) -> int:
        return int(
            run_worker_child(
                config_path=config_path,
                queue_root=queue_root,
                queue_id=queue_id,
                admission_token=admission_token,
            )
        )

    return run_worker_child_job


def build_lazy_queue_worker_runner(
    module_name: str, function_name: str = "main"
) -> QueueWorkerRunner:
    queue_worker_main = _lazy_callable(module_name, function_name)

    def run_queue_worker(argv: list[str]) -> int:
        return int(queue_worker_main(argv))

    return run_queue_worker


def build_worker_child_command_for_engine(engine: str) -> WorkerChildCommandBuilder:
    from .worker_child import build_worker_child_command_for_engine as build_command

    return build_command(engine)


def build_queue_entry_by_id(list_queue: QueueList) -> QueueEntryById:
    return build_queue_entry_lookup(
        list_queue_fn=list_queue,
        coerce_root_to_path=True,
    )


def build_engine_runtime_roots(engine: str) -> RuntimeRootsBuilder:
    engine_id = str(engine).strip().lower()

    def runtime_roots(cfg: Any) -> tuple[Path, ...]:
        return _runtime_roots_for_cfg(cfg, engine=engine_id)

    return runtime_roots


def _default_list_queue(root: str | Path) -> list[Any]:
    from orca_auto.core.queue import list_queue

    return list_queue(root)


def _default_dequeue_next(root: Path) -> Any | None:
    from orca_auto.core.queue import dequeue_next

    return dequeue_next(root)


def _default_dequeue_entry_if_pending(
    root: Path,
    queue_id: str,
    *,
    expected_entry: Any | None = None,
) -> Any | None:
    from orca_auto.core.queue import dequeue_entry_if_pending

    return dequeue_entry_if_pending(root, queue_id, expected_entry=expected_entry)


def build_queue_engine_definition(
    *,
    engine: str,
    load_config: Callable[[str], Any],
    run_worker_child_job: WorkerChildRunner,
    queue_worker_runner: QueueWorkerRunner,
    worker_pid_file_name: str,
    list_queue: QueueList | None = None,
    dequeue_next: QueueDequeuer | None = None,
    dequeue_entry_if_pending: QueueEntryDequeuer | None = None,
    build_worker_child_command: WorkerChildCommandBuilder | None = None,
    runtime_roots_for_cfg: RuntimeRootsBuilder | None = None,
    queue_entry_by_id: QueueEntryById | None = None,
) -> EngineDefinition:
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
