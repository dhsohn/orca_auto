from __future__ import annotations

import signal
from typing import Any, cast

import pytest

from orca_auto import cli_worker_supervision as worker_supervision


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


class _FakeTime:
    def __init__(self) -> None:
        self.current = 0.0
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.current += seconds


def test_spawn_supervised_worker_starts_each_worker_in_a_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeWorkerProcess([None])
    popen_kwargs: dict[str, Any] = {}

    def _fake_popen(*args: Any, **kwargs: Any) -> _FakeWorkerProcess:
        del args
        popen_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(worker_supervision.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(worker_supervision.time, "monotonic", lambda: 42.0)

    managed = worker_supervision._spawn_supervised_worker(
        worker_supervision.WorkerSpec(app="orca", argv=("orca", "worker"))
    )

    assert managed.process is process
    assert managed.started_at_monotonic == 42.0
    assert popen_kwargs["start_new_session"] is True


def test_run_worker_supervisor_staggers_initial_worker_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = [_FakeWorkerProcess([None]), _FakeWorkerProcess([None])]
    sleep_calls: list[float] = []

    monkeypatch.setattr(
        worker_supervision.subprocess,
        "Popen",
        lambda *_args, **_kwargs: processes.pop(0),
    )
    monkeypatch.setattr(worker_supervision.signal, "getsignal", lambda _sig: None)
    monkeypatch.setattr(worker_supervision.signal, "signal", lambda _sig, _handler: None)
    monkeypatch.setattr(worker_supervision.time, "sleep", sleep_calls.append)
    monkeypatch.setattr(
        worker_supervision,
        "_supervise_worker_processes",
        lambda _processes, _shutdown: 0,
    )

    result = worker_supervision._run_worker_supervisor(
        [
            worker_supervision.WorkerSpec(app="workflow", argv=("workflow", "worker")),
            worker_supervision.WorkerSpec(app="orca", argv=("orca", "worker")),
        ],
        startup_stagger_seconds=2.0,
    )

    assert result == 0
    assert sleep_calls == [2.0]


def test_run_worker_supervisor_keeps_siblings_running_after_clean_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    processes = [
        _FakeWorkerProcess([0]),
        _FakeWorkerProcess([None]),
        _FakeWorkerProcess([None]),
    ]
    popen_calls = 0
    installed_handlers: dict[int, Any] = {}
    sleep_calls = 0

    def _fake_popen(*args: Any, **kwargs: Any) -> _FakeWorkerProcess:
        del args, kwargs
        nonlocal popen_calls
        process = processes[popen_calls]
        popen_calls += 1
        return process

    def _fake_signal(sig: int, handler: Any) -> None:
        installed_handlers[sig] = handler

    def _fake_sleep(seconds: float) -> None:
        del seconds
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            installed_handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(worker_supervision.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(worker_supervision.signal, "getsignal", lambda sig: None)
    monkeypatch.setattr(worker_supervision.signal, "signal", _fake_signal)
    monkeypatch.setattr(worker_supervision.time, "sleep", _fake_sleep)

    result = worker_supervision._run_worker_supervisor(
        [
            worker_supervision.WorkerSpec(app="workflow", argv=("workflow", "worker")),
            worker_supervision.WorkerSpec(app="orca", argv=("orca", "worker")),
        ],
        startup_stagger_seconds=0,
    )

    assert result == 0
    assert processes[0].terminate_calls == 0
    assert processes[1].terminate_calls == 1
    assert processes[2].terminate_calls == 1
    assert popen_calls == 3
    out = capsys.readouterr().out
    assert "worker[workflow] exited with code 0" in out
    assert "restarting worker[workflow]: workflow worker" in out


def test_run_worker_supervisor_stops_after_finite_workflow_clean_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    processes = [
        _FakeWorkerProcess([0]),
        _FakeWorkerProcess([None]),
    ]
    popen_calls = 0
    installed_handlers: dict[int, Any] = {}

    def _fake_popen(*args: Any, **kwargs: Any) -> _FakeWorkerProcess:
        del args, kwargs
        nonlocal popen_calls
        process = processes[popen_calls]
        popen_calls += 1
        return process

    def _fake_signal(sig: int, handler: Any) -> None:
        installed_handlers[sig] = handler

    def _fail_sleep(_seconds: float) -> None:
        raise AssertionError("finite workflow clean exit should stop without sleeping")

    monkeypatch.setattr(worker_supervision.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(worker_supervision.signal, "getsignal", lambda sig: None)
    monkeypatch.setattr(worker_supervision.signal, "signal", _fake_signal)
    monkeypatch.setattr(worker_supervision.time, "sleep", _fail_sleep)

    result = worker_supervision._run_worker_supervisor(
        [
            worker_supervision.WorkerSpec(
                app="workflow",
                argv=("workflow", "worker", "--max-cycles", "3"),
                restart_on_clean_exit=False,
            ),
            worker_supervision.WorkerSpec(app="orca", argv=("orca", "worker")),
        ],
        startup_stagger_seconds=0,
    )

    assert result == 0
    assert processes[0].terminate_calls == 0
    assert processes[1].terminate_calls == 1
    assert popen_calls == 2
    out = capsys.readouterr().out
    assert "worker[workflow] exited with code 0" in out
    assert "worker[workflow] completed cleanly; stopping supervisor." in out
    assert "restarting worker[workflow]" not in out


def test_run_worker_supervisor_restarts_workers_after_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    processes = [
        _FakeWorkerProcess([2]),
        _FakeWorkerProcess([None]),
        _FakeWorkerProcess([None]),
    ]
    popen_calls = 0
    installed_handlers: dict[int, Any] = {}
    sleep_calls = 0

    def _fake_popen(*args: Any, **kwargs: Any) -> _FakeWorkerProcess:
        del args, kwargs
        nonlocal popen_calls
        process = processes[popen_calls]
        popen_calls += 1
        return process

    def _fake_signal(sig: int, handler: Any) -> None:
        installed_handlers[sig] = handler

    def _fake_sleep(seconds: float) -> None:
        del seconds
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            installed_handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(worker_supervision.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(worker_supervision.signal, "getsignal", lambda sig: None)
    monkeypatch.setattr(worker_supervision.signal, "signal", _fake_signal)
    monkeypatch.setattr(worker_supervision.time, "sleep", _fake_sleep)

    result = worker_supervision._run_worker_supervisor(
        [
            worker_supervision.WorkerSpec(app="workflow", argv=("workflow", "worker")),
            worker_supervision.WorkerSpec(app="orca", argv=("orca", "worker")),
        ],
        startup_stagger_seconds=0,
    )

    assert result == 0
    assert processes[0].terminate_calls == 0
    assert processes[1].terminate_calls == 1
    assert processes[2].terminate_calls == 1
    assert popen_calls == 3
    out = capsys.readouterr().out
    assert "worker[workflow] exited with code 2" in out
    assert "restarting worker[workflow]: workflow worker" in out


def test_run_worker_supervisor_stops_after_repeated_startup_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    processes = [
        _FakeWorkerProcess([2]),
        _FakeWorkerProcess([None]),
        _FakeWorkerProcess([2]),
    ]
    popen_calls = 0
    installed_handlers: dict[int, Any] = {}
    sleep_calls = 0

    def _fake_popen(*args: Any, **kwargs: Any) -> _FakeWorkerProcess:
        del args, kwargs
        nonlocal popen_calls
        process = processes[popen_calls]
        popen_calls += 1
        return process

    def _fake_signal(sig: int, handler: Any) -> None:
        installed_handlers[sig] = handler

    def _fake_sleep(seconds: float) -> None:
        del seconds
        nonlocal sleep_calls
        sleep_calls += 1

    monkeypatch.setattr(worker_supervision.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(worker_supervision.signal, "getsignal", lambda sig: None)
    monkeypatch.setattr(worker_supervision.signal, "signal", _fake_signal)
    monkeypatch.setattr(worker_supervision.time, "sleep", _fake_sleep)

    result = worker_supervision._run_worker_supervisor(
        [
            worker_supervision.WorkerSpec(app="workflow", argv=("workflow", "worker")),
            worker_supervision.WorkerSpec(app="orca", argv=("orca", "worker")),
        ],
        startup_stagger_seconds=0,
    )

    assert result == 2
    assert processes[0].terminate_calls == 0
    assert processes[1].terminate_calls == 1
    assert processes[2].terminate_calls == 0
    assert popen_calls == 3
    assert sleep_calls == 1
    out = capsys.readouterr().out
    assert "worker[workflow] exited with code 2" in out
    assert (
        "worker[workflow] failed repeatedly during startup; stopping supervisor to avoid a restart loop."
        in out
    )
    assert "restarting worker[workflow]: workflow worker" in out


def test_restart_or_stop_worker_stops_repeated_non_startup_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec = worker_supervision.WorkerSpec(app="orca", argv=("orca", "worker"))
    managed = worker_supervision._SupervisedWorker(
        spec=spec,
        process=cast(Any, _FakeWorkerProcess([2])),
        started_at_monotonic=0.0,
        restart_timestamps=[10.0, 150.0],
    )
    processes = [managed]
    monkeypatch.setattr(
        worker_supervision,
        "_spawn_supervised_worker",
        lambda *_args, **_kwargs: pytest.fail("restart circuit must open before spawning"),
    )

    result = worker_supervision._restart_or_stop_worker(
        processes,
        index=0,
        managed=managed,
        returncode=2,
        current_time=299.0,
    )

    assert result == 2
    assert processes == [managed]
    assert "exited repeatedly within 300 seconds" in capsys.readouterr().out


def test_worker_spec_to_dict_redacts_unrelated_environment_keys() -> None:
    spec = worker_supervision.WorkerSpec(
        app="orca",
        argv=("python", "-m", "orca_auto.orca.commands.queue"),
        cwd="/tmp/orca_auto",
        env={
            "PYTHONPATH": "/tmp/orca_auto/src:/tmp/orca_auto",
            "SECRET_TOKEN": "do-not-print",
        },
    )

    payload = spec.to_dict()

    assert payload["env"] == {"PYTHONPATH": "/tmp/orca_auto/src:/tmp/orca_auto"}


def test_worker_spec_to_dict_omits_empty_allowed_environment() -> None:
    spec = worker_supervision.WorkerSpec(
        app="orca",
        argv=("python", "-m", "orca_auto.orca.commands.queue"),
        env={"SECRET_TOKEN": "do-not-print"},
    )

    assert spec.to_dict()["env"] is None


def test_terminate_process_kills_after_grace_period(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _HangingWorkerProcess()
    fake_time = _FakeTime()
    monkeypatch.setattr(worker_supervision, "time", fake_time)

    worker_supervision._terminate_process(cast(Any, process))

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert fake_time.sleep_calls
