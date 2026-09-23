from __future__ import annotations

from pathlib import Path

from orca_auto.core.engines import (
    build_lazy_queue_worker_runner,
    build_lazy_worker_child_runner,
    build_queue_engine_definition,
    own_engine_accept_entry,
)
from orca_auto.core.queue.types import QueueEntry

from .config import load_config
from .queue.adapter import dequeue_entry_if_pending, dequeue_next, get_entry_by_id, list_queue


def _list_queue(root: str | Path) -> list[QueueEntry]:
    return list_queue(Path(root))


def _dequeue_next(root: Path) -> QueueEntry | None:
    return dequeue_next(root, accept_entry_fn=own_engine_accept_entry("orca"))


def _dequeue_entry(
    root: Path,
    queue_id: str,
    *,
    expected_entry: QueueEntry | None = None,
) -> QueueEntry | None:
    return dequeue_entry_if_pending(root, queue_id, expected_entry=expected_entry)


def _entry_by_id(root: str | Path, queue_id: str) -> QueueEntry | None:
    return get_entry_by_id(Path(root), queue_id)


ENGINE_DEFINITION = build_queue_engine_definition(
    engine="orca",
    load_config=load_config,
    run_worker_child_job=build_lazy_worker_child_runner(
        "orca_auto.orca.worker_execution",
        "run_worker_child_job",
    ),
    queue_worker_runner=build_lazy_queue_worker_runner("orca_auto.orca.commands.queue"),
    list_queue=_list_queue,
    dequeue_next=_dequeue_next,
    dequeue_entry_if_pending=_dequeue_entry,
    queue_entry_by_id=_entry_by_id,
    worker_pid_file_name="queue_worker.pid",
)
ENGINE_RUNTIME = ENGINE_DEFINITION.build_queue_runtime()
build_worker_child_command = ENGINE_DEFINITION.runner_callbacks.build_worker_child_command


read_worker_pid = ENGINE_RUNTIME.read_worker_pid

__all__ = ["ENGINE_DEFINITION", "ENGINE_RUNTIME", "build_worker_child_command", "read_worker_pid"]
