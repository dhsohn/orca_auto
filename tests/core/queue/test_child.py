from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.core.queue import child
from orca_auto.core.queue.child import await_parent_admission_handoff


def test_child_worker_shutdown_controller_tracks_request() -> None:
    controller = child.ChildWorkerShutdownController()

    assert controller.is_requested() is False
    controller.request()
    assert controller.is_requested() is True


def test_parent_handoff_waits_until_slot_owner_matches_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = iter(
        [
            SimpleNamespace(owner_pid=99),
            SimpleNamespace(owner_pid=123),
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr("orca_auto.core.queue.child.get_slot", lambda *_args: next(slots))
    monkeypatch.setattr("orca_auto.core.queue.child.os.getpid", lambda: 123)

    assert await_parent_admission_handoff(
        tmp_path,
        "slot",
        timeout_seconds=1,
        monotonic_fn=lambda: 0,
        sleep_fn=sleeps.append,
    )
    assert sleeps == [0.01]
