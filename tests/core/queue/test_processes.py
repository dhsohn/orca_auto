"""``orca_auto.core.queue.processes``: process-group termination."""

from __future__ import annotations

import os
import subprocess
from typing import Any

import pytest

from orca_auto.core.queue import processes
from orca_auto.core.queue.processes import terminate_process_group
from tests.process_helpers import FakeManagedProcess, missing_process_group


class SequencedPollProcess(FakeManagedProcess):
    """A child whose successive ``poll`` answers are scripted (the leader exits mid-check)."""

    def __init__(self, polls: list[int | None], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._polls = list(polls)

    def poll(self) -> int | None:
        if len(self._polls) > 1:
            return self._polls.pop(0)
        return self._polls[0]


# ---------------------------------------------------------------------------
# terminate_process_group
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _missing_groups_and_pids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "killpg", missing_process_group)
    monkeypatch.setattr(processes, "_pid_exists", lambda _pid: False)


def test_already_terminated() -> None:
    process = FakeManagedProcess(poll_result=0)
    assert terminate_process_group(process) is True
    assert process.terminate_calls == 0


def test_terminate_success() -> None:
    process = SequencedPollProcess([None, 0, 0], pid=1234)
    assert terminate_process_group(process) is True
    assert (process.terminate_calls, process.kill_calls) == (1, 0)


def test_terminate_ignores_errors() -> None:
    process = SequencedPollProcess([None, 0, 0], pid=1234, terminate_error=RuntimeError("nope"))
    assert terminate_process_group(process) is True
    assert process.terminate_calls == 1


def test_terminate_does_not_signal_reused_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    process = SequencedPollProcess([None, 0], pid=1234)
    monkeypatch.setattr(processes, "_pid_exists", lambda _pid: True)
    assert terminate_process_group(process) is True
    assert (process.terminate_calls, process.kill_calls) == (0, 0)


def test_escalate_to_kill() -> None:
    process = FakeManagedProcess(
        pid=1234,
        wait_side_effects=[
            subprocess.TimeoutExpired(cmd="worker", timeout=10),
            subprocess.TimeoutExpired(cmd="worker", timeout=5),
        ],
    )
    assert terminate_process_group(process) is False
    assert (process.terminate_calls, process.kill_calls) == (1, 1)
