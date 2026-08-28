from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto import cli_common
from orca_auto import cli_handlers as cli_run_dir
from orca_auto import cli_worker_supervision as worker_supervision
from orca_auto import cli_workers as unified_cli
from orca_auto import cli_workers as worker_conflicts
from orca_auto import cli_workers as worker_specs


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


def test_worker_module_command_without_repo_root_uses_module_execution() -> None:
    argv, cwd, env = worker_specs.worker_module_command(
        config_path="/tmp/config.yaml",
        repo_root=None,
        module_name="orca_auto.orca.commands.queue",
        tail_argv=["--engine", "orca"],
    )

    assert argv == [
        sys.executable,
        "-m",
        "orca_auto.orca.commands.queue",
        "--config",
        "/tmp/config.yaml",
        "--engine",
        "orca",
    ]
    assert cwd is None
    assert env is None


def test_worker_module_command_with_repo_root_uses_module_execution_and_prepends_pythonpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setenv("PYTHONPATH", "/existing/site-packages")

    argv, cwd, env = worker_specs.worker_module_command(
        config_path="/tmp/config.yaml",
        repo_root=str(repo_root),
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
    assert cwd == str(repo_root.resolve())
    assert env is not None
    assert env["PYTHONPATH"] == f"{repo_root.resolve()}:/existing/site-packages"


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

    result = unified_cli.cmd_queue_worker(
        SimpleNamespace(app=["orca"], workflow_root=None, orca_auto_config=None, json=False)
    )

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
        SimpleNamespace(
            app=["orca"], workflow_root=None, orca_auto_config="/tmp/orca_auto.yaml", json=False
        )
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


def test_worker_command_and_selection_helpers_cover_edges() -> None:
    assert worker_conflicts._read_process_command(999999999) == ()
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
            [worker_supervision.WorkerSpec(app="workflow", argv=("workflow", "worker"))],
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
