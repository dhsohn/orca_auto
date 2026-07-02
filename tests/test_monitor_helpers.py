from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orca_auto.orca.commands import _helpers as command_helpers
from orca_auto.orca.commands import monitor
from orca_auto.orca.config import AppConfig, PathsConfig, RuntimeConfig, TelegramConfig
from orca_auto.orca.dft.monitor import MonitorResult, ScanReport


def _cfg(allowed_root: Path, *, telegram_enabled: bool = True) -> AppConfig:
    telegram = (
        TelegramConfig(bot_token="token", chat_id="1234") if telegram_enabled else TelegramConfig()
    )
    return AppConfig(
        runtime=RuntimeConfig(
            allowed_root=str(allowed_root), organized_root=str(allowed_root.parent / "outputs")
        ),
        paths=PathsConfig(orca_executable="/usr/bin/orca"),
        telegram=telegram,
    )


def test_default_config_path_prefers_environment_variable(monkeypatch) -> None:
    monkeypatch.setenv(command_helpers.CONFIG_ENV_VAR, "/tmp/custom_orca_auto.yaml")
    assert command_helpers.default_config_path() == "/tmp/custom_orca_auto.yaml"


def test_validate_root_scan_dir_rejects_non_directory_and_mismatch(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    organized_root = tmp_path / "organized"
    allowed_root.mkdir()
    organized_root.mkdir()
    cfg = _cfg(allowed_root)
    cfg.runtime.organized_root = str(organized_root)

    bad_file = tmp_path / "file.txt"
    bad_file.write_text("x", encoding="utf-8")
    try:
        command_helpers._validate_root_scan_dir(cfg, str(bad_file))
    except ValueError as exc:
        assert "Root directory not found" in str(exc)
    else:
        raise AssertionError("Expected ValueError for non-directory root")

    nested = allowed_root / "nested"
    nested.mkdir()
    try:
        command_helpers._validate_root_scan_dir(cfg, str(nested))
    except ValueError as exc:
        assert "--root must exactly match allowed_root" in str(exc)
    else:
        raise AssertionError("Expected ValueError for mismatched root")


def test_human_bytes_and_emit_cover_formatting_and_selected_keys(capsys) -> None:
    assert command_helpers._human_bytes(512) == "512.0 B"
    assert command_helpers._human_bytes(2048) == "2.0 KB"
    assert command_helpers._human_bytes(1024**4) == "1.0 TB"

    command_helpers._emit(
        {
            "status": "completed",
            "reaction_dir": "/tmp/rxn",
            "reason": "done",
            "ignored": "value",
        }
    )

    output = capsys.readouterr().out
    assert "status: completed" in output
    assert "job_dir: /tmp/rxn" in output
    assert "reason: done" in output
    assert "ignored" not in output


def test_run_monitor_fails_without_telegram_or_allowed_root(tmp_path: Path) -> None:
    disabled_cfg = _cfg(tmp_path / "runs_disabled", telegram_enabled=False)
    assert monitor._run_monitor(disabled_cfg) == 1

    missing_root_cfg = _cfg(tmp_path / "missing", telegram_enabled=True)
    assert monitor._run_monitor(missing_root_cfg) == 1


def test_run_monitor_returns_one_when_notification_fails(tmp_path: Path) -> None:
    allowed_root = tmp_path / "orca_runs"
    allowed_root.mkdir()
    cfg = _cfg(allowed_root, telegram_enabled=True)
    fake_monitor = SimpleNamespace(
        scan=lambda: ScanReport(new_results=[MonitorResult()], scanned_files=1)
    )

    with (
        patch("orca_auto.orca.commands.monitor.DFTIndex") as index_cls,
        patch(
            "orca_auto.orca.commands.monitor.DFTMonitor",
            return_value=fake_monitor,
        ),
        patch(
            "orca_auto.orca.commands.monitor.has_monitor_updates",
            return_value=True,
        ),
        patch(
            "orca_auto.orca.commands.monitor.notify_monitor_report",
            return_value=False,
        ),
    ):
        assert monitor._run_monitor(cfg) == 1

    index_cls.return_value.initialize.assert_called_once_with(str(allowed_root / "dft.db"))


def test_cmd_monitor_returns_status_for_loaded_config(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "orca_runs", telegram_enabled=True)
    args = SimpleNamespace(config="config.yml")

    with (
        patch("orca_auto.orca.commands.monitor.load_config", return_value=cfg) as load_config_mock,
        patch(
            "orca_auto.orca.commands.monitor._run_monitor",
            return_value=7,
        ) as run_monitor_mock,
    ):
        assert monitor.cmd_monitor(args) == 7

    load_config_mock.assert_called_once_with("config.yml")
    run_monitor_mock.assert_called_once_with(cfg)
