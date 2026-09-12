from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto import cli as unified_cli
from orca_auto import cli_handlers as cli_run_dir
from orca_auto import (
    cli_queue,
    cli_systemd_apply,
    cli_systemd_restart,
    cli_systemd_status,
)
from tests.config_discovery_helpers import isolate_shared_config_discovery


@pytest.fixture(autouse=True)
def _isolate_shared_config_discovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_shared_config_discovery(monkeypatch, tmp_path)


def test_main_without_command_prints_help(capsys) -> None:
    result = unified_cli.main([])

    assert result == 0
    out = capsys.readouterr().out
    assert "usage: orca_auto" in out


def test_main_does_not_mask_broken_pipe_before_handler_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    side_effects: list[str] = []

    def closed_pipe() -> None:
        raise BrokenPipeError("downstream closed before mutation")

    def handler(_args: SimpleNamespace) -> int:
        closed_pipe()
        side_effects.append("mutated")
        return 0

    parser = SimpleNamespace(
        parse_args=lambda _argv: SimpleNamespace(no_color=False, func=handler),
        print_help=lambda: None,
    )
    monkeypatch.setattr(unified_cli, "build_parser", lambda: parser)
    monkeypatch.setattr(unified_cli, "_silence_broken_stdout", lambda: None)

    with pytest.raises(BrokenPipeError, match="before mutation"):
        unified_cli.main([])

    assert side_effects == []


def test_main_preserves_handler_result_when_only_final_flush_hits_broken_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClosedPipe:
        def flush(self) -> None:
            raise BrokenPipeError("downstream closed after handler")

        def fileno(self) -> int:
            raise OSError("no file descriptor")

    parser = SimpleNamespace(
        parse_args=lambda _argv: SimpleNamespace(no_color=False, func=lambda _args: 7),
        print_help=lambda: None,
    )
    monkeypatch.setattr(unified_cli, "build_parser", lambda: parser)
    monkeypatch.setattr(unified_cli, "sys", SimpleNamespace(stdout=ClosedPipe()))

    assert unified_cli.main([]) == 7


def test_main_preserves_handler_result_when_output_breaks_after_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    side_effects: list[str] = []

    class ClosedPipe:
        def write(self, _text: str) -> int:
            raise BrokenPipeError("downstream closed after mutation")

        def flush(self) -> None:
            raise BrokenPipeError("downstream closed after mutation")

        def fileno(self) -> int:
            raise OSError("no file descriptor")

    def handler(_args: SimpleNamespace) -> int:
        side_effects.append("mutated")
        unified_cli.sys.stdout.write("success payload\n")
        return 7

    parser = SimpleNamespace(
        parse_args=lambda _argv: SimpleNamespace(no_color=False, func=handler),
        print_help=lambda: None,
    )
    monkeypatch.setattr(unified_cli, "build_parser", lambda: parser)
    monkeypatch.setattr(unified_cli, "sys", SimpleNamespace(stdout=ClosedPipe()))

    assert unified_cli.main([]) == 7
    assert side_effects == ["mutated"]


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


def test_build_parser_parses_index_prune() -> None:
    parser = unified_cli.build_parser()

    args = parser.parse_args(
        ["index", "prune", "--config", "/tmp/orca_auto.yaml", "--apply", "--json"]
    )
    assert args.command == "index"
    assert args.index_command == "prune"
    assert args.orca_auto_config == "/tmp/orca_auto.yaml"
    assert args.apply is True
    assert args.json is True
    assert args.func is cli_run_dir.cmd_index_prune

    dry_args = parser.parse_args(["index", "prune"])
    assert dry_args.apply is False
    assert dry_args.json is False


def _write_index_prune_fixture(tmp_path: Path) -> tuple[Path, Path]:
    runs_root = tmp_path / "runs"
    live_dir = runs_root / "live"
    live_dir.mkdir(parents=True)
    gone_dir = runs_root / "gone"
    rows = [
        {
            "job_id": "job-live",
            "app_name": "orca_auto_orca",
            "job_type": "orca_opt",
            "status": "completed",
            "original_run_dir": str(live_dir),
            "latest_known_path": str(live_dir),
        },
        {
            "job_id": "job-gone",
            "app_name": "orca_auto_orca",
            "job_type": "orca_ts",
            "status": "running",
            "original_run_dir": str(gone_dir),
            "latest_known_path": str(gone_dir / "20260423-091429-ab9f5aea"),
        },
    ]
    index_path = runs_root / "job_locations.json"
    index_path.write_text(json.dumps(rows), encoding="utf-8")
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")
    return index_path, config_path


def test_cmd_index_prune_dry_run_lists_rows_without_writing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    index_path, config_path = _write_index_prune_fixture(tmp_path)
    before = index_path.read_bytes()

    assert unified_cli.main(["index", "prune", "--config", str(config_path)]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"index: {index_path}" in captured.out
    assert "rows: 2" in captured.out
    assert "prunable: 1" in captured.out
    assert "by status: running 1" in captured.out
    assert "job-gone" in captured.out
    assert "job-live" not in captured.out
    assert "dry run: pass --apply" in captured.out
    assert index_path.read_bytes() == before


def test_cmd_index_prune_apply_rewrites_the_index_and_reports_json(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    index_path, config_path = _write_index_prune_fixture(tmp_path)

    assert (
        unified_cli.main(["index", "prune", "--config", str(config_path), "--apply", "--json"]) == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["index_path"] == str(index_path)
    assert payload["total"] == 2
    assert payload["pruned_count"] == 1
    assert payload["applied"] is True
    assert [row["job_id"] for row in payload["pruned"]] == ["job-gone"]
    remaining = json.loads(index_path.read_text(encoding="utf-8"))
    assert [row["job_id"] for row in remaining] == ["job-live"]


def test_cmd_index_prune_rejects_a_missing_runs_root(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(f"runs_root: {tmp_path / 'absent'}\n", encoding="utf-8")

    assert unified_cli.main(["index", "prune", "--config", str(config_path), "--apply"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "runs_root does not exist" in captured.err


@pytest.mark.parametrize(
    ("config_text", "expected"),
    [
        ("runs_root: [unclosed\n", "Invalid YAML"),
        (None, "No such file"),
    ],
)
def test_cmd_index_prune_names_a_damaged_or_missing_config(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    config_text: str | None,
    expected: str,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    if config_text is not None:
        config_path.write_text(config_text, encoding="utf-8")

    assert unified_cli.main(["index", "prune", "--config", str(config_path), "--apply"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert expected in captured.err
    assert "not configured" not in captured.err
    assert "Traceback" not in captured.err


def test_cmd_index_prune_reports_a_damaged_index_without_writing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    index_path, config_path = _write_index_prune_fixture(tmp_path)
    index_path.write_text("{not valid json", encoding="utf-8")

    assert unified_cli.main(["index", "prune", "--config", str(config_path), "--apply"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert "Traceback" not in captured.err
    assert index_path.read_text(encoding="utf-8") == "{not valid json"


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
    shortcut_scaffold_args = parser.parse_args(
        ["scaffold", "conformer_search", "/tmp/conformer-inputs"]
    )

    assert init_args.command == "init"
    assert init_args.force is True
    assert init_args.func is cli_run_dir.cmd_init

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
    assert restart_args.force is False

    forced_restart_args = parser.parse_args(["service", "restart", "--force"])
    assert forced_restart_args.force is True
    assert forced_restart_args.func is cli_systemd_restart.cmd_service_restart


def test_service_help_describes_idle_only_restart(capsys: pytest.CaptureFixture[str]) -> None:
    parser = unified_cli.build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["service", "--help"])

    assert exc_info.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "Restart services only when their calculation admission pools are idle." in output
    assert "queue worker service" not in output


def test_service_restart_help_warns_about_force(capsys: pytest.CaptureFixture[str]) -> None:
    parser = unified_cli.build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["service", "restart", "--help"])
    assert exc_info.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "--force" in output
    assert "running calculations may be interrupted" in output


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
