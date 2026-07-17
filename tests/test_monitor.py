"""Monitor command tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from orca_auto.core.messaging import render_telegram
from orca_auto.orca.config import AppConfig, PathsConfig, RetryRuntimeConfig, TelegramConfig
from orca_auto.orca.dft.monitor import MonitorResult, ParseFailure, ScanReport
from orca_auto.orca.notifications import (
    _monitor_dft_groups,
    _monitor_failure_groups,
    monitor_message,
)


def _render_monitor(report: ScanReport, **kwargs: object) -> str:
    return render_telegram(monitor_message(report, **kwargs))  # type: ignore[arg-type]


def _sample_report(n: int = 1) -> ScanReport:
    results = [
        MonitorResult(
            formula="C6H6",
            method_basis="B3LYP/6-31G(d)",
            energy="E = -232.123456 Eh",
            status="completed",
            calc_type="opt",
            path="orca_runs/rxn/calc.out",
            note="",
        )
        for _ in range(n)
    ]
    return ScanReport(new_results=results, scanned_files=5)


def _sample_running_report() -> ScanReport:
    return ScanReport(
        new_results=[
            MonitorResult(
                formula="C6H6",
                method_basis="B3LYP/6-31G(d)",
                energy="E = -232.123456 Eh",
                status="running",
                calc_type="neb",
                path="orca_runs/rxn/calc.out",
                note="",
            )
        ],
        scanned_files=5,
    )


class TestMonitorDftGroups:
    def test_empty_report_yields_no_groups(self) -> None:
        report = ScanReport(new_results=[], scanned_files=5)
        assert _monitor_dft_groups(report) == []

    def test_dft_section_content(self) -> None:
        rendered = _render_monitor(_sample_report())
        assert "New Calculations Detected" in rendered
        assert "C6H6" in rendered
        assert "B3LYP/6-31G(d)" in rendered

    def test_running_only_results_are_suppressed(self) -> None:
        report = _sample_running_report()
        assert _monitor_dft_groups(report) == []


class TestMonitorFailureGroups:
    def test_no_failures_yields_no_groups(self) -> None:
        report = ScanReport(new_results=[], scanned_files=5)
        assert _monitor_failure_groups(report) == []

    def test_failure_section_content(self) -> None:
        report = ScanReport(
            new_results=[],
            failures=[
                ParseFailure(
                    path="orca_runs/job/calc.out",
                    error="invalid literal for float()",
                    error_type="ValueError",
                ),
            ],
            scanned_files=5,
        )
        rendered = _render_monitor(report)
        assert "Scan Parse Failures" in rendered
        assert "ValueError" in rendered


class TestBuildMessage:
    def test_contains_header_scope_and_divider(self) -> None:
        message = _render_monitor(ScanReport(new_results=[], scanned_files=0))
        assert "orca_auto" in message
        assert "scan-notify" in message
        assert "\u2500" in message
        assert "Filesystem discovery only" in message
        assert "run-dir alerts" in message

    def test_header_uses_local_timezone(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        message = _render_monitor(
            ScanReport(new_results=[], scanned_files=0),
            now=datetime(2026, 3, 10, 21, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        )
        assert "<code>" in message
        assert "KST" in message

    def test_includes_dft_and_failure_sections(self) -> None:
        report = ScanReport(
            new_results=_sample_report().new_results,
            failures=[
                ParseFailure(
                    path="job/calc.out", error="bad encoding", error_type="UnicodeDecodeError"
                )
            ],
            scanned_files=1,
        )
        message = _render_monitor(report)
        assert "New Calculations Detected" in message
        assert "Scan Parse Failures" in message
        assert "UnicodeDecodeError" in message


class TestRunMonitor:
    def test_returns_error_when_telegram_not_configured(self) -> None:
        from orca_auto.orca.commands.monitor import _run_monitor

        cfg = AppConfig(
            runtime=RetryRuntimeConfig(allowed_root="/tmp/missing"),
            paths=PathsConfig(orca_executable="/usr/bin/orca"),
            telegram=TelegramConfig(),
        )

        assert _run_monitor(cfg) == 1

    def test_returns_error_when_allowed_root_missing(self) -> None:
        from orca_auto.orca.commands.monitor import _run_monitor

        cfg = AppConfig(
            runtime=RetryRuntimeConfig(allowed_root="/tmp/definitely_missing_monitor_root"),
            paths=PathsConfig(orca_executable="/usr/bin/orca"),
            telegram=TelegramConfig(bot_token="fake", chat_id="123"),
        )

        assert _run_monitor(cfg) == 1

    @patch("orca_auto.orca.commands.monitor.notify_monitor_report", return_value=True)
    @patch("orca_auto.orca.commands.monitor.DFTMonitor")
    @patch("orca_auto.orca.commands.monitor.DFTIndex")
    def test_does_not_send_when_no_discoveries(
        self,
        mock_index_cls: MagicMock,
        mock_monitor_cls: MagicMock,
        mock_notify: MagicMock,
    ) -> None:
        mock_monitor = MagicMock()
        mock_monitor.scan.return_value = ScanReport(new_results=[], scanned_files=0)
        mock_monitor_cls.return_value = mock_monitor

        with tempfile.TemporaryDirectory() as td:
            allowed = Path(td) / "orca_runs"
            allowed.mkdir()

            from orca_auto.orca.commands.monitor import _run_monitor

            cfg = AppConfig(
                runtime=RetryRuntimeConfig(allowed_root=str(allowed)),
                paths=PathsConfig(orca_executable="/usr/bin/orca"),
                telegram=TelegramConfig(bot_token="fake", chat_id="123"),
            )
            result = _run_monitor(cfg)

        assert result == 0
        mock_notify.assert_not_called()

    @patch("orca_auto.orca.commands.monitor.notify_monitor_report", return_value=False)
    @patch("orca_auto.orca.commands.monitor.DFTMonitor")
    @patch("orca_auto.orca.commands.monitor.DFTIndex")
    def test_returns_error_when_notification_send_fails(
        self,
        mock_index_cls: MagicMock,
        mock_monitor_cls: MagicMock,
        mock_notify: MagicMock,
    ) -> None:
        mock_monitor = MagicMock()
        mock_monitor.scan.return_value = _sample_report()
        mock_monitor_cls.return_value = mock_monitor

        with tempfile.TemporaryDirectory() as td:
            allowed = Path(td) / "orca_runs"
            allowed.mkdir()

            from orca_auto.orca.commands.monitor import _run_monitor

            cfg = AppConfig(
                runtime=RetryRuntimeConfig(allowed_root=str(allowed)),
                paths=PathsConfig(orca_executable="/usr/bin/orca"),
                telegram=TelegramConfig(bot_token="fake", chat_id="123"),
            )
            result = _run_monitor(cfg)

        assert result == 1
        mock_index_cls.return_value.initialize.assert_called_once_with(str(allowed / "dft.db"))
        mock_notify.assert_called_once()

    @patch("orca_auto.orca.commands.monitor.notify_monitor_report", return_value=True)
    @patch("orca_auto.orca.commands.monitor.DFTMonitor")
    @patch("orca_auto.orca.commands.monitor.DFTIndex")
    def test_sends_when_new_dft_discovery_exists(
        self,
        mock_index_cls: MagicMock,
        mock_monitor_cls: MagicMock,
        mock_notify: MagicMock,
    ) -> None:
        mock_monitor = MagicMock()
        mock_monitor.scan.return_value = _sample_report()
        mock_monitor_cls.return_value = mock_monitor

        with tempfile.TemporaryDirectory() as td:
            allowed = Path(td) / "orca_runs"
            allowed.mkdir()

            from orca_auto.orca.commands.monitor import _run_monitor

            cfg = AppConfig(
                runtime=RetryRuntimeConfig(allowed_root=str(allowed)),
                paths=PathsConfig(orca_executable="/usr/bin/orca"),
                telegram=TelegramConfig(bot_token="fake", chat_id="123"),
            )
            result = _run_monitor(cfg)

        assert result == 0
        mock_notify.assert_called_once()

    @patch("orca_auto.orca.commands.monitor.notify_monitor_report", return_value=True)
    @patch("orca_auto.orca.commands.monitor.DFTMonitor")
    @patch("orca_auto.orca.commands.monitor.DFTIndex")
    def test_does_not_send_when_only_running_dft_updates_exist(
        self,
        mock_index_cls: MagicMock,
        mock_monitor_cls: MagicMock,
        mock_notify: MagicMock,
    ) -> None:
        mock_monitor = MagicMock()
        mock_monitor.scan.return_value = _sample_running_report()
        mock_monitor_cls.return_value = mock_monitor

        with tempfile.TemporaryDirectory() as td:
            allowed = Path(td) / "orca_runs"
            allowed.mkdir()

            from orca_auto.orca.commands.monitor import _run_monitor

            cfg = AppConfig(
                runtime=RetryRuntimeConfig(allowed_root=str(allowed)),
                paths=PathsConfig(orca_executable="/usr/bin/orca"),
                telegram=TelegramConfig(bot_token="fake", chat_id="123"),
            )
            result = _run_monitor(cfg)

        assert result == 0
        mock_notify.assert_not_called()
