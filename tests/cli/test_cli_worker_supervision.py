from __future__ import annotations

import signal
from typing import Any, cast

import pytest

from orca_auto import cli_worker_supervision as worker_supervision
from orca_auto.core.queue.processes import (
    GRACEFUL_TIMEOUT_SECONDS,
    KILL_TIMEOUT_SECONDS,
    SHUTDOWN_MARGIN_SECONDS,
    SHUTDOWN_POLL_LATENCY_SECONDS,
    worker_shutdown_budget_seconds,
)


class _FakeWorkerProcess:
    def __init__(self, poll_values: list[int | None]) -> None:
        self._poll_values = list(poll_values)
        self._terminal_returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        if self._terminal_returncode is not None:
            return self._terminal_returncode
        if self._poll_values:
            value = self._poll_values.pop(0)
            if value is not None:
                self._terminal_returncode = value
            return value
        return None

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._poll_values.clear()
        self._terminal_returncode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self._poll_values.clear()
        self._terminal_returncode = -9


class _HangingWorkerProcess:
    def __init__(self) -> None:
        self.terminate_calls = 0
        self.kill_calls = 0
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1
        self._returncode = -9


class _SlowStoppingWorkerProcess:
    """A worker parent still stopping its children: it exits ``exits_after`` seconds after SIGTERM."""

    def __init__(self, clock: _FakeTime, *, exits_after: float) -> None:
        self._clock = clock
        self._exits_after = exits_after
        self._terminated_at: float | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self._returncode: int | None = None

    def poll(self) -> int | None:
        if self._returncode is None and self._terminated_at is not None:
            if self._clock.monotonic() - self._terminated_at >= self._exits_after:
                self._returncode = 0
        return self._returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._terminated_at = self._clock.monotonic()

    def kill(self) -> None:
        self.kill_calls += 1
        self._returncode = -9


class _FakeTime:
    def __init__(self) -> None:
        self.current = 0.0
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.current += seconds


def _supervise(
    monkeypatch: pytest.MonkeyPatch,
    processes: list[Any],
    *,
    sigterm_on_sleep: int | None = None,
    clock: _FakeTime | None = None,
    stop_timeout_seconds: float = 30.0,
) -> tuple[int, list[tuple[Any, dict[str, Any]]], int]:
    """Run the supervisor over ``processes``; SIGTERM arrives on sleep ``sigterm_on_sleep``."""
    spawned: list[tuple[Any, dict[str, Any]]] = []
    installed_handlers: dict[int, Any] = {}
    sleeps = 0

    def _fake_popen(argv: Any, **kwargs: Any) -> Any:
        spawned.append((argv, kwargs))
        return processes[len(spawned) - 1]

    def _fake_sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if clock is not None:
            clock.sleep(seconds)
        if sleeps == sigterm_on_sleep:
            installed_handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(worker_supervision.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(worker_supervision.signal, "getsignal", lambda sig: None)
    monkeypatch.setattr(
        worker_supervision.signal,
        "signal",
        lambda sig, handler: installed_handlers.__setitem__(sig, handler),
    )
    monkeypatch.setattr(worker_supervision.time, "sleep", _fake_sleep)
    if clock is not None:
        monkeypatch.setattr(worker_supervision.time, "monotonic", clock.monotonic)

    result = worker_supervision.run_worker_supervisor(
        ("orca", "worker"), stop_timeout_seconds=stop_timeout_seconds
    )
    return result, spawned, sleeps


def test_the_worker_starts_in_its_own_session(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = _FakeWorkerProcess([None])

    result, spawned, _sleeps = _supervise(monkeypatch, [process], sigterm_on_sleep=1)

    assert result == 0
    assert spawned == [(["orca", "worker"], {"start_new_session": True})]
    assert process.terminate_calls == 1
    assert capsys.readouterr().out == "starting worker[orca]: orca worker\n"


def test_the_worker_is_restarted_after_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    processes = [_FakeWorkerProcess([0]), _FakeWorkerProcess([None])]

    result, spawned, _sleeps = _supervise(monkeypatch, processes, sigterm_on_sleep=1)

    assert result == 0
    assert len(spawned) == 2
    assert processes[0].terminate_calls == 0
    assert processes[1].terminate_calls == 1
    assert capsys.readouterr().out == (
        "starting worker[orca]: orca worker\n"
        "worker[orca] exited with code 0\n"
        "restarting worker[orca]: orca worker\n"
    )


def test_the_worker_is_restarted_after_one_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    processes = [_FakeWorkerProcess([2]), _FakeWorkerProcess([None])]

    result, spawned, _sleeps = _supervise(monkeypatch, processes, sigterm_on_sleep=1)

    assert result == 0
    assert len(spawned) == 2
    assert processes[1].terminate_calls == 1
    out = capsys.readouterr().out
    assert "worker[orca] exited with code 2" in out
    assert "restarting worker[orca]: orca worker" in out


def test_the_supervisor_stops_after_repeated_startup_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    processes = [_FakeWorkerProcess([2]), _FakeWorkerProcess([3])]

    result, spawned, sleeps = _supervise(monkeypatch, processes)

    assert result == 3
    assert len(spawned) == 2
    assert sleeps == 1
    assert capsys.readouterr().out.endswith(
        "worker[orca] exited with code 3\n"
        "worker[orca] failed repeatedly during startup; stopping supervisor to avoid a restart loop.\n"
    )


def test_the_supervisor_stops_after_three_exits_within_the_restart_window(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Each worker runs past the startup window before it fails, so only the
    # restart cap (three exits within 300 s) can stop the supervisor.
    processes = [_FakeWorkerProcess([*[None] * 6, 2]) for _ in range(3)]

    result, spawned, _sleeps = _supervise(monkeypatch, processes, clock=_FakeTime())

    assert result == 2
    assert len(spawned) == 3
    out = capsys.readouterr().out
    assert "failed repeatedly during startup" not in out
    assert out.endswith(
        "worker[orca] exited repeatedly within 300 seconds; "
        "stopping supervisor to avoid a restart loop.\n"
    )


def test_exits_further_apart_than_the_restart_window_keep_restarting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failure every 200 s leaves at most two exits inside any 300 s window.
    processes = [_FakeWorkerProcess([*[None] * 200, 2]) for _ in range(4)]
    processes.append(_FakeWorkerProcess([None]))

    result, spawned, _sleeps = _supervise(
        monkeypatch, processes, clock=_FakeTime(), sigterm_on_sleep=4 * 201 + 1
    )

    assert result == 0
    assert len(spawned) == 5
    assert processes[-1].terminate_calls == 1


def test_a_clean_exit_during_shutdown_is_not_restarted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    processes = [_FakeWorkerProcess([None, 0])]

    result, spawned, _sleeps = _supervise(monkeypatch, processes, sigterm_on_sleep=1)

    assert result == 0
    assert len(spawned) == 1
    assert processes[0].terminate_calls == 0
    assert "restarting" not in capsys.readouterr().out


def test_terminate_process_kills_after_grace_period(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _HangingWorkerProcess()
    fake_time = _FakeTime()
    monkeypatch.setattr(worker_supervision, "time", fake_time)

    worker_supervision._terminate_process(cast(Any, process), stop_timeout_seconds=10.0)

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert fake_time.sleep_calls
    assert fake_time.current >= 10.0


def test_worker_shutdown_budget_covers_every_child_stop_plus_requeue() -> None:
    per_child = GRACEFUL_TIMEOUT_SECONDS + KILL_TIMEOUT_SECONDS
    fixed = SHUTDOWN_POLL_LATENCY_SECONDS + SHUTDOWN_MARGIN_SECONDS
    assert worker_shutdown_budget_seconds(1) == per_child + fixed
    assert worker_shutdown_budget_seconds(4) == 4 * per_child + fixed
    assert worker_shutdown_budget_seconds(0) == worker_shutdown_budget_seconds(1)
    # The supervisor's stop budget follows the configured concurrency; a config
    # that did not load gets the default scheduler concurrency's budget.
    assert worker_supervision.worker_stop_budget_seconds(2) == worker_shutdown_budget_seconds(2)
    assert worker_supervision.worker_stop_budget_seconds(None) == worker_shutdown_budget_seconds(4)


def test_terminate_process_waits_for_the_worker_shutdown_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One child ignores SIGTERM for its whole graceful period, so the worker
    # parent only finishes stopping it and requeueing its row after
    # SIGTERM + SIGKILL: longer than the old fixed 10 s supervisor wait.
    fake_time = _FakeTime()
    monkeypatch.setattr(worker_supervision, "time", fake_time)
    exits_after = GRACEFUL_TIMEOUT_SECONDS + KILL_TIMEOUT_SECONDS + 2.0
    budget = worker_shutdown_budget_seconds(1)
    assert 10.0 < exits_after < budget

    process = _SlowStoppingWorkerProcess(fake_time, exits_after=exits_after)
    worker_supervision._terminate_process(cast(Any, process), stop_timeout_seconds=budget)

    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.poll() == 0

    # The old budget would have killed the parent mid-shutdown.
    old_process = _SlowStoppingWorkerProcess(fake_time, exits_after=exits_after)
    worker_supervision._terminate_process(cast(Any, old_process), stop_timeout_seconds=10.0)
    assert old_process.kill_calls == 1


def test_run_worker_supervisor_gives_the_worker_its_shutdown_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeTime()
    exits_after = GRACEFUL_TIMEOUT_SECONDS + KILL_TIMEOUT_SECONDS + 2.0
    process = _SlowStoppingWorkerProcess(clock, exits_after=exits_after)

    result, _spawned, _sleeps = _supervise(
        monkeypatch,
        [process],
        clock=clock,
        sigterm_on_sleep=1,
        stop_timeout_seconds=worker_shutdown_budget_seconds(1),
    )

    assert result == 0
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
