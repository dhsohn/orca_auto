from __future__ import annotations

from typing import Any

from orca_auto.orca.commands import queue as queue_worker_entrypoint


def test_orca_queue_worker_entrypoint_parses_worker_args(monkeypatch) -> None:
    worker_calls: list[Any] = []

    def _worker(args: Any) -> int:
        worker_calls.append(args)
        return 41

    monkeypatch.setattr(queue_worker_entrypoint, "cmd_queue_worker", _worker)

    assert (
        queue_worker_entrypoint.main(
            [
                "--config",
                "/tmp/orca_auto.yaml",
            ]
        )
        == 41
    )

    assert len(worker_calls) == 1
    assert worker_calls[0].config == "/tmp/orca_auto.yaml"
