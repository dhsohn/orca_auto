from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from orca_auto.core.queue.child import execution as child_execution
from orca_auto.core.queue.types import QueueEntry


def test_child_worker_shutdown_controller_tracks_request() -> None:
    controller = child_execution.ChildWorkerShutdownController()

    assert controller.is_requested() is False
    controller.request()
    assert controller.is_requested() is True


def test_find_queue_entry_by_id_returns_matching_entry(tmp_path: Path) -> None:
    wanted = QueueEntry(
        queue_id="q-wanted",
        app_name="orca_auto_orca",
        task_id="task-wanted",
        task_kind="orca_run_inp",
        engine="orca",
    )
    entries = [replace(wanted, queue_id="q-other"), wanted]

    assert (
        child_execution.find_queue_entry_by_id(
            tmp_path,
            "q-wanted",
            list_queue_fn=lambda _root: entries,
        )
        is wanted
    )
    assert (
        child_execution.find_queue_entry_by_id(
            tmp_path,
            "missing",
            list_queue_fn=lambda _root: entries,
        )
        is None
    )
