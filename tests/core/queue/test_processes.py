"""``orca_auto.core.queue.processes``: spawning children and process-group termination."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
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
# start_background_process
# ---------------------------------------------------------------------------


def test_start_background_process_uses_detached_devnull_popen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    expected = object()

    def fake_popen(command: list[str], **kwargs: object) -> object:
        calls.append({"command": command, **kwargs})
        return expected

    monkeypatch.setattr(processes.subprocess, "Popen", fake_popen)

    assert processes.start_background_process(("python", "-m", "worker")) is expected
    assert calls == [
        {
            "command": ["python", "-m", "worker"],
            "stdout": processes.subprocess.DEVNULL,
            "stderr": processes.subprocess.DEVNULL,
            "stdin": processes.subprocess.DEVNULL,
            "start_new_session": True,
            "text": True,
        }
    ]


def test_start_background_process_redirects_output_to_log_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    expected = object()
    log_path = tmp_path / "logs" / "queue-1.log"

    def fake_popen(command: list[str], **kwargs: object) -> object:
        calls.append({"command": command, **kwargs})
        stdout = kwargs["stdout"]
        descriptor = getattr(stdout, "name", None)
        assert isinstance(descriptor, int)
        assert Path(f"/proc/self/fd/{descriptor}").resolve() == log_path.resolve()
        assert not bool(getattr(stdout, "closed", True))
        return expected

    monkeypatch.setattr(processes.subprocess, "Popen", fake_popen)

    assert (
        processes.start_background_process(("python", "-m", "worker"), log_path=log_path)
        is expected
    )
    assert log_path.parent.exists()
    assert calls[0]["stderr"] == processes.subprocess.STDOUT
    assert calls[0]["stdin"] == processes.subprocess.DEVNULL
    assert calls[0]["start_new_session"] is True
    assert calls[0]["text"] is True


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
