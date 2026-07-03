from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orca_auto.orca.queue import worker_runtime as queue_worker_runtime


def test_make_running_job_attaches_queue_root(tmp_path: Path) -> None:
    entry = SimpleNamespace(queue_id="queue-1", task_id="task-1", reaction_dir="/tmp/rxn")

    running = queue_worker_runtime.make_running_job(
        queue_root=tmp_path / "queue",
        entry=entry,
        process="process",
        admission_token="slot-1",
        queue_entry_id_fn=lambda item: item.queue_id,
        queue_entry_reaction_dir_fn=lambda item: item.reaction_dir,
        queue_entry_task_id_fn=lambda item: item.task_id,
    )

    assert running.queue_id == "queue-1"
    assert running.reaction_dir == "/tmp/rxn"
    assert running.task_id == "task-1"
    assert running.process == "process"
    assert running.admission_token == "slot-1"
    assert running.__dict__["queue_root"] == tmp_path / "queue"


def test_check_cancel_requests_cancels_and_discards_matching_jobs(tmp_path: Path) -> None:
    job = SimpleNamespace(queue_root=tmp_path / "queue")
    cancelled: list[tuple[str, Any]] = []
    discarded: list[str] = []
    worker = SimpleNamespace(
        _running_jobs=lambda: [("queue-1", job), ("queue-2", job)],
        _discard_running_job=lambda queue_id: discarded.append(queue_id),
    )

    queue_worker_runtime.check_cancel_requests(
        worker,
        get_cancel_requested_fn=lambda _root, queue_id: queue_id == "queue-1",
        job_queue_root_fn=lambda _worker, job_obj: job_obj.queue_root,
        cancel_running_job_fn=lambda _worker, queue_id, job_obj: cancelled.append(
            (queue_id, job_obj)
        ),
    )

    assert cancelled == [("queue-1", job)]
    assert discarded == ["queue-1"]


def test_install_worker_runtime_methods_binds_worker_instance() -> None:
    worker = SimpleNamespace()
    calls: list[tuple[Any, ...]] = []
    job = object()

    queue_worker_runtime.install_worker_runtime_methods(
        worker,
        cancel_running_job_fn=lambda worker_obj, queue_id, job_obj: calls.append(
            ("cancel", worker_obj is worker, queue_id, job_obj is job)
        ),
    )

    worker._cancel_running_job("queue-1", job)

    assert calls == [
        ("cancel", True, "queue-1", True),
    ]
