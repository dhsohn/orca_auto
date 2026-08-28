from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto import cli as unified_cli
from orca_auto import (
    cli_common,
    cli_queue,
    cli_systemd_apply,
    cli_systemd_restart,
    cli_systemd_status,
)
from orca_auto import cli_handlers as cli_run_dir


@pytest.fixture(autouse=True)
def _isolate_shared_config_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explicit_shared_config_path(explicit: str | None) -> str | None:
        if not explicit:
            return None
        return str(Path(explicit).expanduser().resolve())

    monkeypatch.setattr(cli_common, "_discover_shared_config_path", _explicit_shared_config_path)
    monkeypatch.setattr(cli_common, "shared_workflow_root_from_config", lambda config_path: None)


def test_main_without_command_prints_help(capsys) -> None:
    result = unified_cli.main([])

    assert result == 0
    out = capsys.readouterr().out
    assert "usage: orca_auto" in out


def test_build_parser_parses_unified_queue_commands() -> None:
    parser = unified_cli.build_parser()

    list_args = parser.parse_args(
        [
            "queue",
            "list",
            "--engine",
            "xtb",
            "--status",
            "running",
            "--kind",
            "job",
        ]
    )
    assert list_args.command == "queue"
    assert list_args.queue_command == "list"
    assert list_args.engine == ["xtb"]
    assert list_args.status == ["running"]
    assert list_args.kind == ["job"]
    assert list_args.func is cli_queue.cmd_queue_list

    clear_args = parser.parse_args(["queue", "list", "clear", "--json"])
    assert clear_args.command == "queue"
    assert clear_args.queue_command == "list"
    assert clear_args.action == "clear"
    assert clear_args.json is True
    assert clear_args.func is cli_queue.cmd_queue_list

    cancel_args = parser.parse_args(["queue", "cancel", "xtb-q-1"])
    assert cancel_args.queue_command == "cancel"
    assert cancel_args.target == "xtb-q-1"
    assert cancel_args.func is cli_queue.cmd_queue_cancel


@pytest.mark.parametrize("removed_args", [["--watch"], ["--interval", "1"]])
def test_build_parser_rejects_removed_queue_watch_options(removed_args: list[str]) -> None:
    parser = unified_cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["queue", "list", *removed_args])


def test_build_parser_parses_unified_run_dir_commands() -> None:
    parser = unified_cli.build_parser()

    orca_args = parser.parse_args(
        [
            "run-dir",
            "/tmp/rxn",
            "--config",
            "/tmp/orca_auto.yaml",
            "--verbose",
            "--log-file",
            "/tmp/orca.log",
            "--priority",
            "4",
            "--force",
            "--max-cores",
            "12",
            "--max-memory-gb",
            "48",
        ]
    )
    workflow_args = parser.parse_args(
        [
            "run-dir",
            "/tmp/workflow-inputs",
            "--priority",
            "6",
            "--max-cores",
            "12",
            "--max-memory-gb",
            "48",
            "--json",
        ]
    )

    assert orca_args.command == "run-dir"
    assert orca_args.path == "/tmp/rxn"
    assert orca_args.config == "/tmp/orca_auto.yaml"
    assert orca_args.priority == 4
    assert orca_args.force is True
    assert orca_args.max_cores == 12
    assert orca_args.max_memory_gb == 48
    assert orca_args.func is cli_run_dir.cmd_run_dir

    assert workflow_args.path == "/tmp/workflow-inputs"
    assert workflow_args.priority == 6
    assert not hasattr(workflow_args, "workflow_type")
    assert not hasattr(workflow_args, "workflow_root")
    assert not hasattr(workflow_args, "reactant_xyz")
    assert not hasattr(workflow_args, "product_xyz")
    assert not hasattr(workflow_args, "input_xyz")
    assert workflow_args.max_cores == 12
    assert workflow_args.max_memory_gb == 48
    assert workflow_args.json is True
    assert workflow_args.func is cli_run_dir.cmd_run_dir


def test_run_dir_help_renders_engine_directives(capsys: pytest.CaptureFixture[str]) -> None:
    parser = unified_cli.build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["run-dir", "--help"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert "%pal/%maxcore" in rendered


@pytest.mark.parametrize(
    "removed_option",
    ["--workflow-type", "--workflow-root", "--reactant-xyz", "--product-xyz", "--input-xyz"],
)
def test_run_dir_parser_rejects_internal_workflow_options(removed_option: str) -> None:
    parser = unified_cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["run-dir", "/tmp/workflow-inputs", removed_option, "value"])


def test_build_parser_parses_unified_init_and_scaffold_commands() -> None:
    parser = unified_cli.build_parser()

    init_args = parser.parse_args(["init", "--orca_auto-config", "/tmp/orca_auto.yaml", "--force"])
    ts_scaffold_args = parser.parse_args(["scaffold", "ts_search", "/tmp/workflow-inputs"])
    shortcut_scaffold_args = parser.parse_args(
        ["scaffold", "conformer_search", "/tmp/conformer-inputs"]
    )

    assert init_args.command == "init"
    assert init_args.force is True
    assert init_args.func is cli_run_dir.cmd_init

    assert ts_scaffold_args.command == "scaffold"
    assert ts_scaffold_args.scaffold_app == "ts_search"
    assert ts_scaffold_args.root == "/tmp/workflow-inputs"
    assert ts_scaffold_args.workflow_type == "reaction_ts_search"
    assert getattr(ts_scaffold_args, "crest_mode", None) is None
    assert ts_scaffold_args.func is cli_run_dir.cmd_workflow_scaffold

    assert shortcut_scaffold_args.command == "scaffold"
    assert shortcut_scaffold_args.scaffold_app == "conformer_search"
    assert shortcut_scaffold_args.root == "/tmp/conformer-inputs"
    assert shortcut_scaffold_args.workflow_type == "conformer_screening"
    assert getattr(shortcut_scaffold_args, "crest_mode", None) is None
    assert shortcut_scaffold_args.func is cli_run_dir.cmd_workflow_scaffold


def test_build_parser_parses_systemd_install_command() -> None:
    parser = unified_cli.build_parser()

    args = parser.parse_args(
        [
            "systemd",
            "install",
            "--user",
            "alice",
            "--repo",
            "/home/alice/orca_auto",
            "--worker-only",
            "--dry-run",
        ]
    )

    assert args.command == "systemd"
    assert args.systemd_command == "install"
    assert args.target_user == "alice"
    assert args.repo == "/home/alice/orca_auto"
    assert args.worker_only is True
    assert args.dry_run is True
    assert args.func is cli_systemd_apply.cmd_systemd_install


def test_build_parser_parses_service_commands() -> None:
    parser = unified_cli.build_parser()

    status_args = parser.parse_args(["service", "status"])
    restart_args = parser.parse_args(["service", "restart"])

    assert status_args.command == "service"
    assert status_args.service_command == "status"
    assert status_args.func is cli_systemd_status.cmd_service_status

    assert restart_args.command == "service"
    assert restart_args.service_command == "restart"
    assert restart_args.func is cli_systemd_restart.cmd_service_restart


def test_service_help_describes_engine_worker_restart(capsys: pytest.CaptureFixture[str]) -> None:
    parser = unified_cli.build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["service", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "runtime or engine-worker target" in output
    assert "queue worker service" not in output


def test_main_dispatches_unified_queue_list(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[SimpleNamespace] = []

    def fake_cmd(args: SimpleNamespace) -> int:
        seen.append(args)
        return 17

    monkeypatch.setattr(cli_queue, "cmd_queue_list", fake_cmd)

    result = unified_cli.main(["queue", "list", "--engine", "xtb", "--status", "running"])

    assert result == 17
    assert len(seen) == 1
    assert seen[0].queue_command == "list"
    assert seen[0].engine == ["xtb"]
    assert seen[0].status == ["running"]


def test_main_dispatches_unified_queue_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[SimpleNamespace] = []

    def fake_cmd(args: SimpleNamespace) -> int:
        seen.append(args)
        return 18

    monkeypatch.setattr(cli_queue, "cmd_queue_cancel", fake_cmd)

    result = unified_cli.main(["queue", "cancel", "crest-q-1", "--json"])

    assert result == 18
    assert len(seen) == 1
    assert seen[0].queue_command == "cancel"
    assert seen[0].target == "crest-q-1"
    assert seen[0].json is True


@pytest.mark.parametrize(
    ("argv", "attr_name", "expected_attrs", "expected_result"),
    [
        (
            ["run-dir", "/tmp/rxn", "--orca_auto-config", "/tmp/orca_auto.yaml", "--priority", "3"],
            "cmd_run_dir",
            {"command": "run-dir", "path": "/tmp/rxn", "priority": 3},
            21,
        ),
        (
            ["init", "--orca_auto-config", "/tmp/orca_auto.yaml", "--force"],
            "cmd_init",
            {"command": "init", "force": True},
            22,
        ),
        (
            ["scaffold", "conformer_search", "/tmp/workflow-job"],
            "cmd_workflow_scaffold",
            {
                "command": "scaffold",
                "scaffold_app": "conformer_search",
                "root": "/tmp/workflow-job",
                "workflow_type": "conformer_screening",
            },
            24,
        ),
    ],
)
def test_main_dispatches_unified_engine_commands(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    attr_name: str,
    expected_attrs: dict[str, Any],
    expected_result: int,
) -> None:
    seen: list[SimpleNamespace] = []

    def fake_cmd(args: SimpleNamespace) -> int:
        seen.append(args)
        return expected_result

    monkeypatch.setattr(cli_run_dir, attr_name, fake_cmd)

    result = unified_cli.main(argv)

    assert result == expected_result
    assert len(seen) == 1
    for key, expected_value in expected_attrs.items():
        assert getattr(seen[0], key) == expected_value
