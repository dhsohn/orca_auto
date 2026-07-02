from __future__ import annotations

from pathlib import Path

from orca_auto.core.engines import (
    build_lazy_queue_worker_runner,
    build_lazy_worker_child_runner,
    build_queue_engine_definition,
)

from .config import load_config
from .queue.adapter import dequeue_entry_if_pending, dequeue_next, list_queue
from .telegram_notifier import notify_run_finished_event

ENGINE_DEFINITION = build_queue_engine_definition(
    engine="orca",
    load_config=load_config,
    run_worker_child_job=build_lazy_worker_child_runner(
        "orca_auto.orca.worker_execution",
        "run_worker_child_job",
    ),
    queue_worker_runner=build_lazy_queue_worker_runner("orca_auto.orca.commands.queue"),
    list_queue=lambda root: list_queue(Path(root)),
    dequeue_next=lambda root: dequeue_next(root),
    dequeue_entry_if_pending=lambda root, queue_id: dequeue_entry_if_pending(Path(root), queue_id),
    worker_pid_file_name="queue_worker.pid",
    job_finished=notify_run_finished_event,
)
build_worker_child_command = ENGINE_DEFINITION.build_worker_child_command


__all__ = ["ENGINE_DEFINITION", "build_worker_child_command"]
