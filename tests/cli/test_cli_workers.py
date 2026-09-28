from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto import cli_handlers, cli_run_dir, cli_workers
from orca_auto import cli_worker_supervision as worker_supervision
from orca_auto.core.config.schema import SchedulerConfig
from orca_auto.core.queue.processes import worker_shutdown_budget_seconds
from orca_auto.core.queue.worker.pid_file import write_worker_pid_file


@pytest.mark.parametrize(
    "name",
    [
        "WORKER_APP",
        "_SupervisorShutdown",
        "_install_supervisor_signal_handlers",
        "_spawn_worker",
        "_terminate_process",
        "run_worker_supervisor",
        "worker_stop_budget_seconds",
    ],
)
def test_cli_workers_does_not_forward_supervision_symbols(name: str) -> None:
    assert not hasattr(cli_workers, name)


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


def test_cmd_queue_worker_json_names_the_one_worker_with_the_config_budget(
    tmp_path: Path, fake_orca: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_worker_config(tmp_path, fake_orca, max_active=2)

    assert cli_workers.cmd_queue_worker(SimpleNamespace(config=str(config), json=True)) == 0

    # The supervisor's own interpreter runs the worker module; no checkout
    # PYTHONPATH or environment of its own is injected.
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "workers": [
            {
                "app": "orca",
                "argv": [
                    sys.executable,
                    "-m",
                    "orca_auto.orca.commands.queue",
                    "--config",
                    str(config.resolve()),
                ],
                "cwd": "",
                "env": None,
                "restart_on_clean_exit": True,
                "stop_timeout_seconds": worker_shutdown_budget_seconds(2),
            }
        ],
    }


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
    fake_orca: Path,
) -> None:
    target = tmp_path / "orca_job"
    target.mkdir()
    (target / "job.inp").write_text("! Opt\n", encoding="utf-8")
    captured: list[tuple[str | None, str]] = []

    monkeypatch.setattr(cli_run_dir, "_configure_orca_logging", lambda args: None)
    discovered = _write_worker_config(tmp_path, fake_orca)
    discovered.write_text(discovered.read_text().replace(str(tmp_path / "runs"), str(tmp_path)))
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

    result = cli_run_dir.cmd_run_dir(
        argparse.Namespace(
            path=str(target),
            config=None,
            verbose=False,
            log_file=None,
        )
    )

    assert result == 31
    assert captured == [(str(discovered), str(target))]


def test_cmd_queue_worker_supervises_the_worker_with_its_stop_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_orca: Path
) -> None:
    config = _write_worker_config(tmp_path, fake_orca, max_active=3)
    started: list[tuple[list[str], float]] = []

    def fake_supervisor(argv: Sequence[str], *, stop_timeout_seconds: float) -> int:
        started.append((list(argv), stop_timeout_seconds))
        return 7

    monkeypatch.setattr(worker_supervision, "run_worker_supervisor", fake_supervisor)

    assert cli_workers.cmd_queue_worker(SimpleNamespace(config=str(config), json=False)) == 7
    assert started == [
        (
            [sys.executable, "-m", "orca_auto.orca.commands.queue", "--config", str(config)],
            worker_shutdown_budget_seconds(3),
        )
    ]


def test_cmd_queue_worker_refuses_a_live_worker_for_the_same_runs_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_orca: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _write_worker_config(tmp_path, fake_orca)
    runs = (tmp_path / "runs").resolve()
    # This test process stands in for the worker that already owns runs_root.
    write_worker_pid_file(runs)
    monkeypatch.setattr(
        worker_supervision,
        "run_worker_supervisor",
        lambda argv, **kwargs: pytest.fail("a second worker must not start"),
    )

    result = cli_workers.cmd_queue_worker(SimpleNamespace(config=str(config), json=False))

    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        f"error: existing ORCA queue worker detected for allowed_root {runs} "
        f"(pid={os.getpid()}). command: " in captured.err
    )
    assert "hint: Stop the existing worker before starting another worker." in captured.err


def test_worker_command_helpers_cover_edges() -> None:
    assert cli_workers._read_process_command(999999999) == ()
    assert cli_workers._format_command_argv(()) == "<unavailable>"
    assert cli_workers._format_command_argv(("python", "-m", "orca_auto.cli")) == (
        "python -m orca_auto.cli"
    )
