from __future__ import annotations

from typing import Any

import pytest

from orca_auto.core.queue.child import execution as child_execution
from orca_auto.flow.engines.xtb import execution as worker_job


def test_worker_job_main_parses_queue_identity_args(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_worker_job(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 37

    monkeypatch.setattr(worker_job, "run_worker_job", fake_run_worker_job)

    result = worker_job.main(
        [
            "--config",
            "/tmp/orca_auto.yaml",
            "--queue-root",
            "/tmp/queue",
            "--queue-id",
            "q-1",
            "--admission-token",
            " slot-1 ",
        ]
    )

    assert result == 37
    assert captured["config_path"] == "/tmp/orca_auto.yaml"
    assert captured["queue_root"] == "/tmp/queue"
    assert captured["queue_id"] == "q-1"
    assert captured["admission_token"] == "slot-1"


def test_worker_job_install_shutdown_handlers_wires_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[Any] = []
    controller = child_execution.ChildWorkerShutdownController()

    monkeypatch.setattr(
        worker_job,
        "install_shutdown_signal_handlers",
        lambda callback: installed.append(callback),
    )

    install = worker_job.shutdown_signal_handler_installer(
        worker_job.install_shutdown_signal_handlers
    )
    install(controller)

    assert controller.is_requested() is False
    installed[0]()
    assert controller.is_requested() is True
