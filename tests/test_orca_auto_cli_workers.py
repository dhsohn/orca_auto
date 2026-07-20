from __future__ import annotations

import argparse
import json
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orca_auto import cli_common
from orca_auto import cli_handlers as cli_run_dir
from orca_auto import cli_worker_conflicts as worker_conflicts
from orca_auto import cli_worker_specs as worker_specs
from orca_auto import cli_workers as unified_cli


@pytest.fixture(autouse=True)
def _isolate_shared_config_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explicit_shared_config_path(explicit: str | None) -> str | None:
        if not explicit:
            return None
        return str(Path(explicit).expanduser().resolve())

    monkeypatch.setattr(worker_specs, "_discover_shared_config_path", _explicit_shared_config_path)
    monkeypatch.setattr(
        worker_conflicts, "_discover_shared_config_path", _explicit_shared_config_path
    )
    monkeypatch.setattr(cli_common, "_discover_shared_config_path", _explicit_shared_config_path)
    monkeypatch.setattr(cli_common, "shared_workflow_root_from_config", lambda config_path: None)


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


def test_build_worker_specs_defaults_to_orca_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker_specs, "_discover_shared_config_path", lambda explicit: "/tmp/orca_auto.yaml"
    )

    def fake_worker_module_command(
        *,
        config_path: str,
        repo_root: str | None,
        module_name: str,
        tail_argv: list[str],
    ) -> tuple[list[str], str | None, dict[str, str] | None]:
        del repo_root
        return (["python", "-m", module_name, "--config", config_path, *tail_argv], None, {})

    monkeypatch.setattr(worker_specs, "worker_module_command", fake_worker_module_command)

    specs = worker_specs._build_worker_specs(
        SimpleNamespace(app=None, workflow_root=None, orca_auto_config=None)
    )

    assert [spec.app for spec in specs] == ["orca"]
    assert str(specs[0].argv[2]) == "orca_auto.core.engines.queue_worker"
    assert specs[0].argv[-2:] == ("--engine", "orca")
    assert specs[0].env is not None
    assert specs[0].env == {}


def test_build_worker_specs_runs_each_selected_default_engine_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker_specs, "_discover_shared_config_path", lambda explicit: "/tmp/orca_auto.yaml"
    )

    def fake_worker_module_command(
        *,
        config_path: str,
        repo_root: str | None,
        module_name: str,
        tail_argv: list[str],
    ) -> tuple[list[str], str | None, dict[str, str] | None]:
        del repo_root
        return (["python", "-m", module_name, "--config", config_path, *tail_argv], None, {})

    monkeypatch.setattr(worker_specs, "worker_module_command", fake_worker_module_command)

    specs = worker_specs._build_worker_specs(
        SimpleNamespace(
            app=["orca", "orca"],
            workflow_root=None,
            orca_auto_config=None,
        )
    )

    assert [spec.app for spec in specs] == ["orca"]
    assert [spec.argv[-2:] for spec in specs] == [
        ("--engine", "orca"),
    ]


def test_build_worker_specs_does_not_infer_workflow_workers_from_configured_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker_specs, "_discover_shared_config_path", lambda explicit: "/tmp/orca_auto.yaml"
    )
    monkeypatch.setattr(
        cli_common, "shared_workflow_root_from_config", lambda config_path: "/tmp/workflows"
    )

    def fake_worker_module_command(
        *,
        config_path: str,
        repo_root: str | None,
        module_name: str,
        tail_argv: list[str],
    ) -> tuple[list[str], str | None, dict[str, str] | None]:
        del repo_root
        return (["python", "-m", module_name, "--config", config_path, *tail_argv], None, {})

    monkeypatch.setattr(worker_specs, "worker_module_command", fake_worker_module_command)

    specs = worker_specs._build_worker_specs(
        SimpleNamespace(app=None, workflow_root=None, orca_auto_config=None)
    )

    assert [spec.app for spec in specs] == ["orca"]
    assert str(specs[0].argv[2]) == "orca_auto.core.engines.queue_worker"
    assert specs[0].argv[-2:] == ("--engine", "orca")


def test_build_worker_specs_requires_workflow_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker_specs, "_discover_shared_config_path", lambda explicit: "/tmp/orca_auto.yaml"
    )
    with pytest.raises(ValueError, match="workflow worker requires runs_root in orca_auto.yaml"):
        worker_specs._build_worker_specs(
            SimpleNamespace(app=["workflow"], workflow_root=None, orca_auto_config=None)
        )


def test_build_worker_specs_explicit_workflow_app_uses_configured_workflow_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker_specs, "_discover_shared_config_path", lambda explicit: "/tmp/orca_auto.yaml"
    )
    monkeypatch.setattr(
        cli_common, "shared_workflow_root_from_config", lambda config_path: "/tmp/workflows"
    )

    specs = worker_specs._build_worker_specs(
        SimpleNamespace(app=["workflow"], workflow_root=None, orca_auto_config=None)
    )

    assert [spec.app for spec in specs] == ["crest", "xtb", "workflow"]
    assert str(specs[0].argv[2]) == "orca_auto.core.engines.queue_worker"
    assert str(specs[1].argv[2]) == "orca_auto.core.engines.queue_worker"
    assert specs[0].argv[-2:] == ("--engine", "crest")
    assert specs[1].argv[-2:] == ("--engine", "xtb")
    assert "--workflow-root" in specs[2].argv
    assert "/tmp/workflows" in specs[2].argv


def test_workflow_root_for_args_uses_shared_config(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    monkeypatch.setattr(
        cli_common, "_discover_shared_config_path", lambda explicit: "/tmp/orca_auto.yaml"
    )

    def _shared_workflow_root(config_path: str | None) -> str:
        seen.append(config_path)
        return "/tmp/from-config-workflows"

    monkeypatch.setattr(
        cli_common,
        "shared_workflow_root_from_config",
        _shared_workflow_root,
    )

    discovered = cli_common._workflow_root_for_args(
        SimpleNamespace(
            workflow_root=None,
            orca_auto_config=None,
            config=None,
        )
    )

    assert discovered == "/tmp/from-config-workflows"
    assert seen == ["/tmp/orca_auto.yaml"]


def test_engine_config_for_command_uses_discovered_shared_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_common, "_discover_shared_config_path", lambda explicit: "/tmp/orca_auto.yaml"
    )

    discovered = cli_common._engine_config_for_command(
        argparse.Namespace(
            orca_auto_config=None,
            config=None,
        )
    )

    assert discovered == str(Path("/tmp/orca_auto.yaml").resolve())


def test_cmd_orca_run_dir_uses_discovered_shared_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "orca_job"
    target.mkdir()
    (target / "job.inp").write_text("! Opt\n", encoding="utf-8")
    captured: list[tuple[str | None, str]] = []

    monkeypatch.setattr(cli_run_dir, "_configure_orca_logging", lambda args: None)
    monkeypatch.setattr(
        cli_common, "_discover_shared_config_path", lambda explicit: "/tmp/orca_auto.yaml"
    )

    import orca_auto.orca.commands.run_inp as run_inp_cmd

    def _fake_cmd_run_inp(args: argparse.Namespace) -> int:
        captured.append((getattr(args, "config", None), getattr(args, "path", "")))
        return 31

    monkeypatch.setattr(
        run_inp_cmd,
        "cmd_run_inp",
        _fake_cmd_run_inp,
    )

    result = cli_run_dir.cmd_orca_run_dir(
        argparse.Namespace(
            path=str(target),
            orca_auto_config=None,
            config=None,
            verbose=False,
            log_file=None,
        )
    )

    assert result == 31
    assert captured == [(str(Path("/tmp/orca_auto.yaml").resolve()), str(target))]


def test_cmd_queue_worker_returns_supervisor_status(monkeypatch: pytest.MonkeyPatch) -> None:
    specs = [
        unified_cli.WorkerSpec(
            app="orca",
            argv=("python", "-m", "orca_auto.orca.commands.queue"),
        )
    ]
    monkeypatch.setattr(unified_cli, "_build_worker_specs", lambda args: specs)
    monkeypatch.setattr(
        unified_cli, "_run_worker_supervisor", lambda built_specs: 0 if built_specs == specs else 1
    )

    result = unified_cli.cmd_queue_worker(
        SimpleNamespace(app=["orca"], workflow_root=None, orca_auto_config=None, json=False)
    )

    assert result == 0


def test_cmd_queue_worker_reports_existing_orca_auto_orca_worker_conflict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    specs = [
        unified_cli.WorkerSpec(
            app="orca",
            argv=("python", "-m", "orca_auto.orca.commands.queue"),
        )
    ]
    monkeypatch.setattr(unified_cli, "_build_worker_specs", lambda args: specs)
    monkeypatch.setattr(
        unified_cli,
        "_detect_existing_orca_worker_conflict",
        lambda built_specs, args: worker_conflicts._ExistingWorkerConflict(
            app="orca",
            pid=3589996,
            allowed_root="/home/user/orca_runs",
            source="orca_auto",
            command="/home/user/orca_auto/.venv/bin/python -m orca_auto.orca.commands.queue --config /tmp/orca_auto.yaml",
        ),
    )
    monkeypatch.setattr(unified_cli, "_run_worker_supervisor", lambda built_specs: 99)

    result = unified_cli.cmd_queue_worker(
        SimpleNamespace(
            app=["orca"], workflow_root=None, orca_auto_config="/tmp/orca_auto.yaml", json=False
        )
    )

    assert result == 1
    out = capsys.readouterr().out
    assert "existing ORCA queue worker detected" in out
    assert "source: orca_auto queue worker" in out
    assert "Stop the existing queue-worker service before starting another worker." in out


def test_spawn_supervised_worker_starts_each_worker_in_a_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeWorkerProcess([None])
    popen_kwargs: dict[str, Any] = {}

    def _fake_popen(*args: Any, **kwargs: Any) -> _FakeWorkerProcess:
        del args
        popen_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(unified_cli.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(unified_cli.time, "monotonic", lambda: 42.0)

    managed = unified_cli._spawn_supervised_worker(
        unified_cli.WorkerSpec(app="orca", argv=("orca", "worker"))
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
        unified_cli.subprocess,
        "Popen",
        lambda *_args, **_kwargs: processes.pop(0),
    )
    monkeypatch.setattr(unified_cli.signal, "getsignal", lambda _sig: None)
    monkeypatch.setattr(unified_cli.signal, "signal", lambda _sig, _handler: None)
    monkeypatch.setattr(unified_cli.time, "sleep", sleep_calls.append)
    monkeypatch.setattr(
        unified_cli,
        "_supervise_worker_processes",
        lambda _processes, _shutdown: 0,
    )

    result = unified_cli._run_worker_supervisor(
        [
            unified_cli.WorkerSpec(app="workflow", argv=("workflow", "worker")),
            unified_cli.WorkerSpec(app="orca", argv=("orca", "worker")),
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

    monkeypatch.setattr(unified_cli.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(unified_cli.signal, "getsignal", lambda sig: None)
    monkeypatch.setattr(unified_cli.signal, "signal", _fake_signal)
    monkeypatch.setattr(unified_cli.time, "sleep", _fake_sleep)

    result = unified_cli._run_worker_supervisor(
        [
            unified_cli.WorkerSpec(app="workflow", argv=("workflow", "worker")),
            unified_cli.WorkerSpec(app="orca", argv=("orca", "worker")),
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

    monkeypatch.setattr(unified_cli.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(unified_cli.signal, "getsignal", lambda sig: None)
    monkeypatch.setattr(unified_cli.signal, "signal", _fake_signal)
    monkeypatch.setattr(unified_cli.time, "sleep", _fail_sleep)

    result = unified_cli._run_worker_supervisor(
        [
            unified_cli.WorkerSpec(
                app="workflow",
                argv=("workflow", "worker", "--max-cycles", "3"),
                restart_on_clean_exit=False,
            ),
            unified_cli.WorkerSpec(app="orca", argv=("orca", "worker")),
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

    monkeypatch.setattr(unified_cli.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(unified_cli.signal, "getsignal", lambda sig: None)
    monkeypatch.setattr(unified_cli.signal, "signal", _fake_signal)
    monkeypatch.setattr(unified_cli.time, "sleep", _fake_sleep)

    result = unified_cli._run_worker_supervisor(
        [
            unified_cli.WorkerSpec(app="workflow", argv=("workflow", "worker")),
            unified_cli.WorkerSpec(app="orca", argv=("orca", "worker")),
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

    monkeypatch.setattr(unified_cli.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(unified_cli.signal, "getsignal", lambda sig: None)
    monkeypatch.setattr(unified_cli.signal, "signal", _fake_signal)
    monkeypatch.setattr(unified_cli.time, "sleep", _fake_sleep)

    result = unified_cli._run_worker_supervisor(
        [
            unified_cli.WorkerSpec(app="workflow", argv=("workflow", "worker")),
            unified_cli.WorkerSpec(app="orca", argv=("orca", "worker")),
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
    spec = unified_cli.WorkerSpec(app="orca", argv=("orca", "worker"))
    managed = unified_cli._SupervisedWorker(
        spec=spec,
        process=cast(Any, _FakeWorkerProcess([2])),
        started_at_monotonic=0.0,
        restart_timestamps=[10.0, 150.0],
    )
    processes = [managed]
    monkeypatch.setattr(
        unified_cli,
        "_spawn_supervised_worker",
        lambda *_args, **_kwargs: pytest.fail("restart circuit must open before spawning"),
    )

    result = unified_cli._restart_or_stop_worker(
        processes,
        index=0,
        managed=managed,
        returncode=2,
        current_time=299.0,
    )

    assert result == 2
    assert processes == [managed]
    assert "exited repeatedly within 300 seconds" in capsys.readouterr().out


def test_cmd_queue_worker_json_outputs_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    specs = [
        unified_cli.WorkerSpec(
            app="workflow",
            argv=(
                "python",
                "-m",
                "orca_auto.flow.cli.workflow",
                "--workflow-root",
                "/tmp/workflows",
            ),
        )
    ]
    monkeypatch.setattr(unified_cli, "_build_worker_specs", lambda args: specs)

    result = unified_cli.cmd_queue_worker(
        SimpleNamespace(
            app=["workflow"], workflow_root="/tmp/workflows", orca_auto_config=None, json=True
        )
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["workers"][0]["app"] == "workflow"
    assert payload["workers"][0]["argv"][2] == "orca_auto.flow.cli.workflow"


def test_worker_spec_to_dict_redacts_unrelated_environment_keys() -> None:
    spec = unified_cli.WorkerSpec(
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
    spec = unified_cli.WorkerSpec(
        app="orca",
        argv=("python", "-m", "orca_auto.orca.commands.queue"),
        env={"SECRET_TOKEN": "do-not-print"},
    )

    assert spec.to_dict()["env"] is None


def test_worker_command_and_selection_helpers_cover_edges() -> None:
    assert worker_conflicts._read_process_command(999999999) == ()
    assert worker_conflicts._command_invokes_module(
        ("python", "-m", "orca_auto.cli"), "orca_auto.cli"
    )
    assert not worker_conflicts._command_invokes_module(("python", "-m"), "orca_auto.cli")
    assert not worker_conflicts._command_invokes_module(("python", "-m", "orca_auto.cli"), "")
    assert worker_conflicts._command_program_name(()) == ""
    assert worker_conflicts._format_command_argv(()) == "<unavailable>"
    assert worker_conflicts._format_command_argv(("python", "-m", "orca_auto.cli")) == (
        "python -m orca_auto.cli"
    )
    assert worker_specs._selected_worker_apps(["orca", "orca", "workflow", ""]) == [
        "orca",
        "workflow",
    ]

    with pytest.raises(ValueError, match="Unsupported worker app"):
        worker_specs._selected_worker_apps(["bad-app"])


@pytest.mark.parametrize("app", ["xtb", "crest"])
def test_workflow_engine_workers_are_not_direct_app_selections(app: str) -> None:
    with pytest.raises(ValueError, match=f"Unsupported worker app: {app}"):
        worker_specs._selected_worker_apps([app])


def test_worker_tail_and_workflow_spec_include_optional_flags() -> None:
    assert worker_specs._engine_worker_tail_argv(app="orca") == ["--engine", "orca"]
    assert worker_specs._engine_worker_tail_argv(app="xtb") == ["--engine", "xtb"]

    spec = worker_specs._workflow_worker_spec(
        workflow_root="/tmp/workflows",
        config_path="/tmp/orca_auto.yaml",
        args=argparse.Namespace(
            no_submit=True,
            once=True,
            refresh_registry=True,
            refresh_each_cycle=True,
            max_cycles=3,
            interval_seconds=2.5,
            lock_timeout_seconds=9,
        ),
    )

    assert spec.restart_on_clean_exit is False
    assert spec.argv[1:] == (
        "-m",
        "orca_auto.flow.cli.workflow",
        "--workflow-root",
        str(Path("/tmp/workflows").resolve()),
        "--orca_auto-config",
        str(Path("/tmp/orca_auto.yaml").resolve()),
        "--no-submit",
        "--once",
        "--refresh-registry",
        "--refresh-each-cycle",
        "--max-cycles",
        "3",
        "--interval-seconds",
        "2.5",
        "--lock-timeout-seconds",
        "9.0",
    )

    default_spec = worker_specs._workflow_worker_spec(
        workflow_root="/tmp/workflows",
        config_path="/tmp/orca_auto.yaml",
        args=argparse.Namespace(
            no_submit=False,
            once=False,
            refresh_registry=False,
            refresh_each_cycle=False,
            max_cycles=0,
            interval_seconds=0,
            lock_timeout_seconds=0,
        ),
    )

    assert default_spec.restart_on_clean_exit is True


def test_workflow_only_worker_flags_require_workflow_app() -> None:
    with pytest.raises(ValueError, match="workflow-only worker flags require --app workflow"):
        worker_specs._workflow_only_worker_flag_error(
            SimpleNamespace(
                no_submit=True,
                refresh_registry=False,
                refresh_each_cycle=False,
                max_cycles=0,
                interval_seconds=0,
                lock_timeout_seconds=0,
            )
        )

    assert (
        worker_specs._workflow_only_worker_flag_error(
            SimpleNamespace(
                no_submit=False,
                refresh_registry=False,
                refresh_each_cycle=False,
                max_cycles=2,
                interval_seconds=0,
                lock_timeout_seconds=0,
            )
        )
        == "--max-cycles requires --app workflow"
    )


def test_terminate_process_kills_after_grace_period(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _HangingWorkerProcess()
    fake_time = _FakeTime()
    monkeypatch.setattr(unified_cli, "time", fake_time)

    unified_cli._terminate_process(cast(Any, process))

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert fake_time.sleep_calls


def test_cmd_queue_worker_reports_spec_build_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        unified_cli,
        "_build_worker_specs",
        lambda args: (_ for _ in ()).throw(ValueError("bad worker flags")),
    )

    result = unified_cli.cmd_queue_worker(SimpleNamespace(json=False))

    assert result == 1
    assert capsys.readouterr().err == "error: bad worker flags\n"


def test_detect_existing_orca_worker_conflict_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orca_auto.orca.config as orca_config
    import orca_auto.orca.engine as orca_engine

    args = argparse.Namespace(orca_auto_config="/tmp/orca_auto.yaml")

    assert (
        worker_conflicts._detect_existing_orca_worker_conflict(
            [unified_cli.WorkerSpec(app="workflow", argv=("workflow", "worker"))],
            args=args,
        )
        is None
    )

    monkeypatch.setattr(
        orca_config, "load_config", lambda path: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert (
        worker_conflicts._detect_existing_orca_worker_conflict(
            [unified_cli.WorkerSpec(app="orca", argv=("orca", "worker"))],
            args=args,
        )
        is None
    )

    allowed_root = tmp_path / "orca_runs"
    allowed_root.mkdir()
    monkeypatch.setattr(
        orca_config,
        "load_config",
        lambda path: SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(allowed_root))),
    )
    monkeypatch.setattr(orca_engine, "read_worker_pid", lambda root: None)
    assert (
        worker_conflicts._detect_existing_orca_worker_conflict(
            [unified_cli.WorkerSpec(app="orca", argv=("orca", "worker"))],
            args=args,
        )
        is None
    )

    monkeypatch.setattr(orca_engine, "read_worker_pid", lambda root: 43210)
    monkeypatch.setattr(
        worker_conflicts, "_read_process_command", lambda pid: ("python", "worker.py")
    )
    conflict = worker_conflicts._detect_existing_orca_worker_conflict(
        [unified_cli.WorkerSpec(app="orca", argv=("orca", "worker"))],
        args=args,
    )

    assert conflict == worker_conflicts._ExistingWorkerConflict(
        app="orca",
        pid=43210,
        allowed_root=str(allowed_root.resolve()),
        source="unknown",
        command="python worker.py",
    )
