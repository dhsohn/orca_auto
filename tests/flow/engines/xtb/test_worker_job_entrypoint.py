from __future__ import annotations

from typing import Any

import pytest

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
