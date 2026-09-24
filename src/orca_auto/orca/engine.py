"""The ORCA queue runtime: the one engine's queue functions bound to the shared runtime."""

from __future__ import annotations

from pathlib import Path

from orca_auto.core.engines import own_engine_accept_entry
from orca_auto.core.indexing.roots import runtime_roots_for_cfg
from orca_auto.core.queue.engine.runtime import EngineQueueRuntime
from orca_auto.core.queue.types import QueueEntry

from .config import AppConfig
from .queue.adapter import dequeue_entry_if_pending, dequeue_next, get_entry_by_id, list_queue

WORKER_PID_FILE_NAME = "queue_worker.pid"

_accept_orca_entry = own_engine_accept_entry("orca")


def _runtime_roots(cfg: AppConfig) -> tuple[Path, ...]:
    return runtime_roots_for_cfg(cfg)


def _list_queue(root: str | Path) -> list[QueueEntry]:
    return list_queue(Path(root))


def _dequeue_next(root: Path) -> QueueEntry | None:
    return dequeue_next(root, accept_entry_fn=_accept_orca_entry)


def _dequeue_entry(
    root: Path,
    queue_id: str,
    *,
    expected_entry: QueueEntry | None = None,
) -> QueueEntry | None:
    return dequeue_entry_if_pending(root, queue_id, expected_entry=expected_entry)


def _entry_by_id(root: str | Path, queue_id: str) -> QueueEntry | None:
    return get_entry_by_id(Path(root), queue_id)


ENGINE_RUNTIME: EngineQueueRuntime[AppConfig] = EngineQueueRuntime(
    runtime_roots_for_cfg=_runtime_roots,
    list_queue=_list_queue,
    dequeue_next=_dequeue_next,
    dequeue_entry_if_pending=_dequeue_entry,
    queue_entry_by_id_fn=_entry_by_id,
    worker_pid_file_name=WORKER_PID_FILE_NAME,
    accept_entry_fn=_accept_orca_entry,
)

read_worker_pid = ENGINE_RUNTIME.read_worker_pid

__all__ = ["ENGINE_RUNTIME", "WORKER_PID_FILE_NAME", "read_worker_pid"]
