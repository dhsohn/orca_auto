from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto import cli_handlers as cli_run_dir
from orca_auto import cli_worker_supervision as worker_supervision
from orca_auto import cli_workers as unified_cli
from orca_auto import cli_workers as worker_conflicts
from orca_auto import cli_workers as worker_specs
from orca_auto.core.config import discovery
from tests.config_discovery_helpers import isolate_shared_config_discovery


@pytest.fixture(autouse=True)
def _isolate_shared_config_discovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_shared_config_discovery(monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "name",
    [
        "WorkerSpec",
        "_SupervisedWorker",
        "_SupervisorShutdown",
        "_install_supervisor_signal_handlers",
        "_poll_supervised_workers",
        "_restart_or_stop_worker",
        "_run_worker_supervisor",
        "_spawn_supervised_worker",
        "_supervise_worker_processes",
        "_terminate_process",
        "_terminate_supervised_workers",
    ],
)
def test_cli_workers_does_not_forward_supervision_symbols(name: str) -> None:
    assert not hasattr(unified_cli, name)


def test_worker_module_command_uses_the_supervisor_interpreter_without_pythonpath() -> None:
    # The interpreter that runs the supervisor resolves the installed package;
    # no checkout src/ is injected, so wheel and editable layouts behave alike.
    argv = worker_specs.worker_module_command(
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
    spec = worker_specs._orca_worker_spec(config_path="/tmp/orca_auto.yaml")

    assert spec.app == "orca"
    assert spec.cwd is None
    assert spec.env is None
    assert spec.to_dict()["env"] is None


def test_build_worker_specs_defaults_to_orca_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker_specs, "resolve_shared_config_path", lambda explicit: "/tmp/orca_auto.yaml"
    )

    def fake_worker_module_command(*, config_path: str, module_name: str) -> list[str]:
        return ["python", "-m", module_name, "--config", config_path]

    monkeypatch.setattr(worker_specs, "worker_module_command", fake_worker_module_command)

    specs = worker_specs._build_worker_specs(SimpleNamespace(orca_auto_config=None))

    assert [spec.app for spec in specs] == ["orca"]
    assert specs[0].argv == (
        "python",
        "-m",
        "orca_auto.orca.commands.queue",
        "--config",
        "/tmp/orca_auto.yaml",
    )
    assert specs[0].env is None


def test_build_worker_specs_ignores_a_stale_app_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker_specs, "resolve_shared_config_path", lambda explicit: "/tmp/orca_auto.yaml"
    )
    monkeypatch.setattr(
        worker_specs,
        "worker_module_command",
        lambda **kwargs: ["python", "-m", kwargs["module_name"]],
    )

    specs = worker_specs._build_worker_specs(
        SimpleNamespace(app=["workflow", "xtb"], orca_auto_config=None)
    )

    assert [spec.app for spec in specs] == ["orca"]
    assert specs[0].argv == ("python", "-m", "orca_auto.orca.commands.queue")


def test_build_worker_specs_requires_a_discoverable_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_specs, "resolve_shared_config_path", lambda explicit: None)

    with pytest.raises(ValueError, match="Could not discover orca_auto.yaml"):
        worker_specs._build_worker_specs(SimpleNamespace(orca_auto_config=None))


def test_engine_config_for_args_uses_discovered_shared_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        discovery, "resolve_shared_config_path", lambda explicit: "/tmp/orca_auto.yaml"
    )

    discovered = discovery.engine_config_for_args(
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
        discovery, "resolve_shared_config_path", lambda explicit: "/tmp/orca_auto.yaml"
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
        worker_supervision.WorkerSpec(
            app="orca",
            argv=("python", "-m", "orca_auto.orca.commands.queue"),
        )
    ]
    monkeypatch.setattr(unified_cli, "_build_worker_specs", lambda args: specs)
    monkeypatch.setattr(
        worker_supervision,
        "_run_worker_supervisor",
        lambda built_specs: 0 if built_specs == specs else 1,
    )

    result = unified_cli.cmd_queue_worker(SimpleNamespace(orca_auto_config=None, json=False))

    assert result == 0


def test_cmd_queue_worker_reports_existing_orca_auto_orca_worker_conflict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    specs = [
        worker_supervision.WorkerSpec(
            app="orca",
            argv=("python", "-m", "orca_auto.orca.commands.queue"),
        )
    ]
    monkeypatch.setattr(unified_cli, "_build_worker_specs", lambda args: specs)
    monkeypatch.setattr(
        unified_cli,
        "_detect_existing_orca_worker_conflict",
        lambda built_specs, args: worker_conflicts._ExistingWorkerConflict(
            pid=3589996,
            allowed_root="/home/user/orca_runs",
            command="/home/user/orca_auto/.venv/bin/python -m orca_auto.orca.commands.queue --config /tmp/orca_auto.yaml",
        ),
    )
    monkeypatch.setattr(worker_supervision, "_run_worker_supervisor", lambda built_specs: 99)

    result = unified_cli.cmd_queue_worker(
        SimpleNamespace(orca_auto_config="/tmp/orca_auto.yaml", json=False)
    )

    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: existing ORCA queue worker detected" in captured.err
    assert "-m orca_auto.orca.commands.queue" in captured.err
    assert "hint: Stop the existing worker before starting another worker." in captured.err


def test_cmd_queue_worker_json_outputs_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    specs = [
        worker_supervision.WorkerSpec(
            app="orca",
            argv=(
                "python",
                "-m",
                "orca_auto.orca.commands.queue",
                "--config",
                "/tmp/orca_auto.yaml",
            ),
        )
    ]
    monkeypatch.setattr(unified_cli, "_build_worker_specs", lambda args: specs)

    result = unified_cli.cmd_queue_worker(SimpleNamespace(orca_auto_config=None, json=True))

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["workers"][0]["app"] == "orca"
    assert payload["workers"][0]["argv"][2] == "orca_auto.orca.commands.queue"


def test_worker_command_and_selection_helpers_cover_edges() -> None:
    assert worker_conflicts._read_process_command(999999999) == ()
    assert worker_conflicts._format_command_argv(()) == "<unavailable>"
    assert worker_conflicts._format_command_argv(("python", "-m", "orca_auto.cli")) == (
        "python -m orca_auto.cli"
    )


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
            [worker_supervision.WorkerSpec(app="orca", argv=("orca", "worker"))],
            args=args,
        )
        is None
    )

    monkeypatch.setattr(
        orca_config, "load_config", lambda path: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert (
        worker_conflicts._detect_existing_orca_worker_conflict(
            [worker_supervision.WorkerSpec(app="orca", argv=("orca", "worker"))],
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
            [worker_supervision.WorkerSpec(app="orca", argv=("orca", "worker"))],
            args=args,
        )
        is None
    )

    monkeypatch.setattr(orca_engine, "read_worker_pid", lambda root: 43210)
    monkeypatch.setattr(
        worker_conflicts, "_read_process_command", lambda pid: ("python", "worker.py")
    )
    conflict = worker_conflicts._detect_existing_orca_worker_conflict(
        [worker_supervision.WorkerSpec(app="orca", argv=("orca", "worker"))],
        args=args,
    )

    assert conflict == worker_conflicts._ExistingWorkerConflict(
        pid=43210,
        allowed_root=str(allowed_root.resolve()),
        command="python worker.py",
    )
