from __future__ import annotations

from orca_auto.core.queue.child import execution as child_execution


def test_child_worker_shutdown_controller_tracks_request() -> None:
    controller = child_execution.ChildWorkerShutdownController()

    assert controller.is_requested() is False
    controller.request()
    assert controller.is_requested() is True
