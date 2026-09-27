from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto import cli_handlers, cli_workers
from orca_auto import cli_worker_supervision as worker_supervision
from orca_auto.core.config.schema import SchedulerConfig
from orca_auto.core.queue.processes import worker_shutdown_budget_seconds
from tests.conftest import make_app_cfg


@pytest.mark.parametrize(
    "name",
    [
        "WorkerSpec",
        "_SupervisedWorker",
        "_SupervisorShutdown",
        "_install_supervisor_signal_handlers",
        "_poll_supervised_workers",
        "_restart_or_stop_worker",
        "run_worker_supervisor",
        "_spawn_supervised_worker",
        "_supervise_worker_processes",
        "_terminate_process",
        "_terminate_supervised_workers",
    ],
)
def test_cli_workers_does_not_forward_supervision_symbols(name: str) -> None:
    assert not hasattr(cli_workers, name)


def test_worker_module_command_uses_the_supervisor_interpreter_without_pythonpath() -> None:
    # The interpreter that runs the supervisor resolves the installed package;
    # no checkout src/ is injected, so wheel and editable layouts behave alike.
    argv = cli_workers.worker_module_command(
        config_path="/tmp/config.yaml",
        module_name="orca_auto.cli",
        tail_argv=["queue", "cancel", "job-1"],
    )

    assert argv == [
        sys.executable,
        "-m",
        "orca_auto.cli",
        "--config",
        "/tmp/config.yaml",
        "queue",
        "cancel",
        "job-1",
    ]


def test_orca_worker_spec_inherits_the_supervisor_environment() -> None:
    spec = cli_workers._orca_worker_spec(config_path="/tmp/orca_auto.yaml", cfg=None)

    assert spec.app == "orca"
    assert spec.cwd is None
    assert spec.env is None
    assert spec.to_dict()["env"] is None


def _write_worker_config(tmp_path: Path, fake_orca: Path, max_active: int = 1) -> Path:
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        f"runs_root: {runs}\n"
        f"scheduler:\n  max_active_simulations: {max_active}\n"
        f"orca:\n  paths:\n    orca_executable: {fake_orca}\n",
        encoding="utf-8",
    )
    return config


def test_cmd_queue_worker_json_runs_the_orca_queue_module_with_the_config_budget(
    tmp_path: Path, fake_orca: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_worker_config(tmp_path, fake_orca, max_active=2)

    assert cli_workers.cmd_queue_worker(SimpleNamespace(config=str(config), json=True)) == 0

    [worker] = json.loads(capsys.readouterr().out)["workers"]
    assert worker["app"] == "orca"
    assert worker["argv"] == [
        sys.executable,
        "-m",
        "orca_auto.orca.commands.queue",
        "--config",
        str(config.resolve()),
    ]
    assert worker["stop_timeout_seconds"] == worker_shutdown_budget_seconds(2)


def test_cmd_queue_worker_keeps_the_default_budget_for_a_config_that_does_not_load(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The worker itself then fails at startup on the same config.
    config = tmp_path / "orca_auto.yaml"
    config.write_text("runs_root: [unclosed\n", encoding="utf-8")

    assert cli_workers.cmd_queue_worker(SimpleNamespace(config=str(config), json=True)) == 0

    [worker] = json.loads(capsys.readouterr().out)["workers"]
    assert worker["stop_timeout_seconds"] == worker_shutdown_budget_seconds(
        SchedulerConfig.max_active_simulations
    )


def test_cmd_queue_worker_requires_a_discoverable_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_workers.cmd_queue_worker(SimpleNamespace(config=None, json=False)) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: No orca_auto.yaml found")


def test_cmd_run_dir_uses_discovered_shared_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "orca_job"
    target.mkdir()
    (target / "job.inp").write_text("! Opt\n", encoding="utf-8")
    captured: list[tuple[str | None, str]] = []

    monkeypatch.setattr(cli_handlers, "_configure_orca_logging", lambda args: None)
    discovered = tmp_path / "orca_auto.yaml"
    monkeypatch.setattr(
        cli_handlers, "discover_shared_config_path", lambda explicit: str(discovered)
    )

    import orca_auto.orca.commands.run_inp as run_inp_cmd

    def _fake_cmd_run_inp(args: argparse.Namespace, **_seams: object) -> int:
        captured.append((getattr(args, "config", None), str(Path(args.path).resolve())))
        return 31

    monkeypatch.setattr(
        run_inp_cmd,
        "cmd_run_inp",
        _fake_cmd_run_inp,
    )

    result = cli_handlers.cmd_run_dir(
        argparse.Namespace(
            path=str(target),
            config=None,
            verbose=False,
            log_file=None,
        )
    )

    assert result == 31
    assert captured == [(str(discovered), str(target))]


def test_cmd_queue_worker_returns_supervisor_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_orca: Path
) -> None:
    config = _write_worker_config(tmp_path, fake_orca)
    started: list[list[worker_supervision.WorkerSpec]] = []

    def fake_supervisor(specs: list[worker_supervision.WorkerSpec]) -> int:
        started.append(list(specs))
        return 0

    monkeypatch.setattr(worker_supervision, "run_worker_supervisor", fake_supervisor)

    assert cli_workers.cmd_queue_worker(SimpleNamespace(config=str(config), json=False)) == 0
    [[spec]] = started
    assert spec.argv[-2:] == ("--config", str(config.resolve()))


def test_cmd_queue_worker_refuses_a_live_worker_for_the_same_runs_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_orca: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _write_worker_config(tmp_path, fake_orca)
    monkeypatch.setattr(cli_workers, "read_worker_pid_file", lambda root: 3589996)
    monkeypatch.setattr(
        cli_workers,
        "_read_process_command",
        lambda pid: ("/venv/bin/python", "-m", "orca_auto.orca.commands.queue"),
    )
    monkeypatch.setattr(
        worker_supervision,
        "run_worker_supervisor",
        lambda specs: pytest.fail("a second worker must not start"),
    )

    result = cli_workers.cmd_queue_worker(SimpleNamespace(config=str(config), json=False))

    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        f"error: existing ORCA queue worker detected for allowed_root {(tmp_path / 'runs').resolve()}"
        " (pid=3589996)" in captured.err
    )
    assert "-m orca_auto.orca.commands.queue" in captured.err
    assert "hint: Stop the existing worker before starting another worker." in captured.err


def test_worker_command_and_selection_helpers_cover_edges() -> None:
    assert cli_workers._read_process_command(999999999) == ()
    assert cli_workers._format_command_argv(()) == "<unavailable>"
    assert cli_workers._format_command_argv(("python", "-m", "orca_auto.cli")) == (
        "python -m orca_auto.cli"
    )


def test_detect_existing_orca_worker_conflict_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert cli_workers._detect_existing_orca_worker_conflict(None) is None

    allowed_root = tmp_path / "orca_runs"
    allowed_root.mkdir()
    cfg = make_app_cfg(allowed_root)
    monkeypatch.setattr(cli_workers, "read_worker_pid_file", lambda root: None)
    assert cli_workers._detect_existing_orca_worker_conflict(cfg) is None

    monkeypatch.setattr(cli_workers, "read_worker_pid_file", lambda root: 43210)
    monkeypatch.setattr(cli_workers, "_read_process_command", lambda pid: ("python", "worker.py"))
    conflict = cli_workers._detect_existing_orca_worker_conflict(cfg)

    assert conflict == cli_workers._ExistingWorkerConflict(
        pid=43210,
        allowed_root=str(allowed_root.resolve()),
        command="python worker.py",
    )
