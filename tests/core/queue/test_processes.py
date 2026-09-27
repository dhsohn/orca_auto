"""``orca_auto.core.queue.processes``: process-group termination."""

from __future__ import annotations

import subprocess
from typing import Any

from orca_auto.core.queue.processes import (
    ManagedProcess,
    ProcessGroupTerminationDeps,
    terminate_process_group,
)
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

_NO_LIVE_PIDS = ProcessGroupTerminationDeps(pid_exists=lambda _pid: False)


def _terminate(process: ManagedProcess, deps: ProcessGroupTerminationDeps = _NO_LIVE_PIDS) -> bool:
    return terminate_process_group(process, killpg_fn=missing_process_group, deps=deps)


def test_already_terminated() -> None:
    process = FakeManagedProcess(poll_result=0)
    assert _terminate(process) is True
    assert process.terminate_calls == 0


def test_terminate_success() -> None:
    process = SequencedPollProcess([None, 0, 0], pid=1234)
    assert _terminate(process) is True
    assert (process.terminate_calls, process.kill_calls) == (1, 0)


def test_terminate_ignores_errors() -> None:
    process = SequencedPollProcess([None, 0, 0], pid=1234, terminate_error=RuntimeError("nope"))
    assert _terminate(process) is True
    assert process.terminate_calls == 1


def test_terminate_does_not_signal_reused_pid() -> None:
    process = SequencedPollProcess([None, 0], pid=1234)
    assert _terminate(process, ProcessGroupTerminationDeps(pid_exists=lambda _pid: True)) is True
    assert (process.terminate_calls, process.kill_calls) == (0, 0)


def test_escalate_to_kill() -> None:
    process = FakeManagedProcess(
        pid=1234,
        wait_side_effects=[
            subprocess.TimeoutExpired(cmd="worker", timeout=10),
            subprocess.TimeoutExpired(cmd="worker", timeout=5),
        ],
    )
    assert _terminate(process) is False
    assert (process.terminate_calls, process.kill_calls) == (1, 1)
