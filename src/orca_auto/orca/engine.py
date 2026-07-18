from __future__ import annotations

from pathlib import Path

from orca_auto.core.engines import (
    build_lazy_queue_worker_runner,
    build_lazy_worker_child_runner,
    build_queue_engine_definition,
    own_engine_accept_entry,
)

from .config import load_config
from .notifications import notify_run_finished_event
from .queue.adapter import dequeue_entry_if_pending, dequeue_next, get_entry_by_id, list_queue

ENGINE_DEFINITION = build_queue_engine_definition(
    engine="orca",
    load_config=load_config,
    run_worker_child_job=build_lazy_worker_child_runner(
        "orca_auto.orca.worker_execution",
        "run_worker_child_job",
    ),
    queue_worker_runner=build_lazy_queue_worker_runner("orca_auto.orca.commands.queue"),
    list_queue=lambda root: list_queue(Path(root)),
    dequeue_next=lambda root: dequeue_next(root, accept_entry_fn=own_engine_accept_entry("orca")),
    dequeue_entry_if_pending=lambda root, queue_id, **kwargs: dequeue_entry_if_pending(
        Path(root), queue_id, **kwargs
    ),
    queue_entry_by_id=lambda root, queue_id: get_entry_by_id(Path(root), queue_id),
    worker_pid_file_name="queue_worker.pid",
    job_finished=notify_run_finished_event,
)
build_worker_child_command = ENGINE_DEFINITION.build_worker_child_command


__all__ = ["ENGINE_DEFINITION", "build_worker_child_command"]
