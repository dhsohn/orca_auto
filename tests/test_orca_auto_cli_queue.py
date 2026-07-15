from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto import cli_common, cli_style, terminal_table
from orca_auto import cli_queue as unified_cli
from orca_auto.system_metrics import JobMetrics, SystemMetrics, SystemMetricsSampler

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def test_cmd_queue_list_watch_loops_until_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"emit": 0, "provider": 0}

    monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: True)

    def _emit_once(args: Any, request: Any) -> int:
        del args, request
        calls["emit"] += 1
        return 0

    def _sleep(_interval: float) -> None:
        raise KeyboardInterrupt

    def _provider(_config: str | None) -> dict[str, JobMetrics]:
        calls["provider"] += 1
        return {}

    args = SimpleNamespace(
        action=None,
        orca_auto_config=None,
        limit=0,
        refresh=False,
        engine=None,
        status=None,
        kind=None,
        json=False,
        watch=True,
        interval=2.0,
    )
    deps = unified_cli.QueueCliDeps(
        emit_queue_list_once=_emit_once,
        sleep=_sleep,
        job_metrics_provider=_provider,
    )

    cli_style.set_color_override(True)
    try:
        assert unified_cli.cmd_queue_list(args, deps=deps) == 0
    finally:
        cli_style.set_color_override(None)
    # One render happened before the (mocked) sleep raised KeyboardInterrupt.
    assert calls["emit"] == 1
    assert calls["provider"] == 0  # custom emitters stay isolated from the new metrics path


def test_watch_banner_plain_on_non_tty_matches_legacy() -> None:
    # Non-TTY `--watch` must keep the historical banner byte-for-byte — no spinner
    # glyph and no clock leaking into piped output.
    cli_style.set_color_override(False)
    try:
        assert (
            unified_cli._watch_banner_line("⠋", 2.0)
            == "orca_auto queue list — refresh every 2s · Ctrl-C to exit"
        )
    finally:
        cli_style.set_color_override(None)


def test_watch_banner_styled_with_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: True)
    cli_style.set_color_override(True)
    try:
        line = unified_cli._watch_banner_line(
            "⠋", 2.0, now=datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
        )
    finally:
        cli_style.set_color_override(None)
    plain = _strip_ansi(line)
    assert "⠋ live" in plain
    assert "03:00:00" in plain
    assert "\x1b[" in line


def test_watch_banner_compacts_to_terminal_width(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: True)
    cli_style.set_color_override(True)
    try:
        line = unified_cli._watch_banner_line(
            "⠋",
            2.0,
            now=datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC),
            max_width=40,
        )
    finally:
        cli_style.set_color_override(None)
    plain = _strip_ansi(line)
    assert terminal_table.display_width(plain) <= 40
    assert "⠋ live" in plain and "Ctrl-C" in plain


def test_resource_gauge_line_renders_available_fields() -> None:
    metrics = SystemMetrics(
        cpu_percent=58.0,
        mem_used_bytes=8 * 1024**3,
        mem_total_bytes=32 * 1024**3,
        load1=1.0,
        load5=2.0,
        load15=3.0,
    )
    cli_style.set_color_override(True)
    try:
        line = unified_cli._resource_gauge_line(metrics)
    finally:
        cli_style.set_color_override(None)
    assert line is not None
    plain = _strip_ansi(line)
    assert "CPU" in plain and "58%" in plain
    assert "RAM" in plain and "8.0/32.0G" in plain
    assert "load 1.00 2.00 3.00" in plain
    assert "█" in plain or "░" in plain

    compact = unified_cli._resource_gauge_line(metrics, max_width=60)
    assert compact is not None
    compact_plain = _strip_ansi(compact)
    assert terminal_table.display_width(compact_plain) <= 60
    assert "CPU" in compact_plain and "RAM" in compact_plain and "load" in compact_plain


def test_resource_gauge_line_is_none_when_all_sources_missing() -> None:
    empty = SystemMetrics(
        cpu_percent=None,
        mem_used_bytes=None,
        mem_total_bytes=None,
        load1=None,
        load5=None,
        load15=None,
    )
    assert unified_cli._resource_gauge_line(empty) is None


class _FixedSampler(SystemMetricsSampler):
    def __init__(self, metrics: SystemMetrics) -> None:
        self._metrics = metrics
        self.calls = 0

    def sample(self) -> SystemMetrics | None:
        self.calls += 1
        return self._metrics


def _watch_args() -> SimpleNamespace:
    return SimpleNamespace(
        action=None,
        orca_auto_config=None,
        limit=0,
        refresh=False,
        engine=None,
        status=None,
        kind=None,
        json=False,
        watch=True,
        interval=2.0,
    )


def test_watch_prints_system_resource_gauge_on_tty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: True)

    def _emit(_args: Any, _request: Any) -> int:
        return 0

    def _sleep(_interval: float) -> None:
        raise KeyboardInterrupt

    sampler = _FixedSampler(
        SystemMetrics(
            cpu_percent=58.0,
            mem_used_bytes=8 * 1024**3,
            mem_total_bytes=32 * 1024**3,
            load1=1.0,
            load5=2.0,
            load15=3.0,
        )
    )
    deps = unified_cli.QueueCliDeps(
        emit_queue_list_once=_emit, sleep=_sleep, system_metrics_sampler=sampler
    )
    cli_style.set_color_override(True)
    try:
        assert unified_cli.cmd_queue_list(_watch_args(), deps=deps) == 0
    finally:
        cli_style.set_color_override(None)
    out = _strip_ansi(capsys.readouterr().out)
    assert "CPU" in out and "58%" in out
    assert "8.0/32.0G" in out
    assert "load 1.00 2.00 3.00" in out


def test_watch_omits_resource_gauge_on_force_color_non_tty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _emit(_args: Any, _request: Any) -> int:
        return 0

    def _sleep(_interval: float) -> None:
        raise KeyboardInterrupt

    sampler = _FixedSampler(
        SystemMetrics(
            cpu_percent=58.0,
            mem_used_bytes=8 * 1024**3,
            mem_total_bytes=32 * 1024**3,
            load1=1.0,
            load5=2.0,
            load15=3.0,
        )
    )
    deps = unified_cli.QueueCliDeps(
        emit_queue_list_once=_emit, sleep=_sleep, system_metrics_sampler=sampler
    )
    # FORCE_COLOR can paint a pipe, but it must not trigger terminal sampling or
    # change the historical watch banner.
    cli_style.set_color_override(True)
    try:
        assert unified_cli.cmd_queue_list(_watch_args(), deps=deps) == 0
    finally:
        cli_style.set_color_override(None)
    out = capsys.readouterr().out
    assert sampler.calls == 0
    assert "CPU" not in out and "load 1.00" not in out
    assert "orca_auto queue list — refresh every 2s · Ctrl-C to exit" in out


def test_watch_prints_plain_resource_gauge_on_no_color_tty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: True)

    def _emit(_args: Any, _request: Any) -> int:
        return 0

    frames = 0

    def _sleep(_interval: float) -> None:
        nonlocal frames
        frames += 1
        if frames >= 2:
            raise KeyboardInterrupt

    sampler = _FixedSampler(
        SystemMetrics(
            cpu_percent=58.0,
            mem_used_bytes=8 * 1024**3,
            mem_total_bytes=32 * 1024**3,
            load1=1.0,
            load5=2.0,
            load15=3.0,
        )
    )
    deps = unified_cli.QueueCliDeps(
        emit_queue_list_once=_emit, sleep=_sleep, system_metrics_sampler=sampler
    )
    cli_style.set_color_override(False)
    try:
        assert unified_cli.cmd_queue_list(_watch_args(), deps=deps) == 0
    finally:
        cli_style.set_color_override(None)
    out = capsys.readouterr().out
    assert "CPU" in out and "58%" in out and "8.0/32.0G" in out
    assert out.count("\x1b[2J\x1b[3J\x1b[H") == 2
    assert _ANSI_RE.search(out) is None  # cursor control remains; SGR color does not


def test_layout_interactive_requires_terminal_and_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FORCE_COLOR on a pipe must not enable the human layout, and a real terminal
    # keeps the released plain layout when ANSI painting is disabled.
    monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: False)
    cli_style.set_color_override(True)
    try:
        assert unified_cli._layout_interactive() is False
    finally:
        cli_style.set_color_override(None)

    monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: True)
    cli_style.set_color_override(True)
    try:
        assert unified_cli._layout_interactive() is True
    finally:
        cli_style.set_color_override(None)

    cli_style.set_color_override(False)
    try:
        assert unified_cli._layout_interactive() is False
    finally:
        cli_style.set_color_override(None)


def test_queue_list_stays_plain_under_force_color_pipe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Regression: FORCE_COLOR on a pipe (color on, not a TTY) must keep the
    # machine-readable plain layout — the ``active_simulations:`` line, no summary
    # band / tree glyphs / rail — while still emitting color codes.
    monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: False)
    monkeypatch.setattr(
        unified_cli, "_queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
    )
    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: {
            "count": 3,
            "activities": [
                {
                    "activity_id": "wf-1",
                    "kind": "workflow",
                    "engine": "workflow",
                    "status": "running",
                    "label": "screen",
                    "source": "orca_auto_flow",
                    "submitted_at": "2026-04-26T00:47:00+00:00",
                    "updated_at": "2026-04-26T00:47:00+00:00",
                },
                {
                    "activity_id": "orca-1",
                    "kind": "job",
                    "engine": "orca",
                    "status": "running",
                    "label": "opt",
                    "source": "orca_auto_orca",
                    "parent_workflow_id": "wf-1",
                    "metadata": {"workflow_id": "wf-1"},
                    "submitted_at": "2026-04-26T02:00:00+00:00",
                    "updated_at": "2026-04-26T02:00:00+00:00",
                },
                {
                    "activity_id": "orca-2",
                    "kind": "job",
                    "engine": "orca",
                    "status": "running",
                    "label": "freq",
                    "source": "orca_auto_orca",
                    "parent_workflow_id": "wf-1",
                    "metadata": {"workflow_id": "wf-1"},
                    "submitted_at": "2026-04-26T02:57:00+00:00",
                    "updated_at": "2026-04-26T02:57:00+00:00",
                },
            ],
            "sources": {},
        },
    )
    cli_style.set_color_override(True)  # FORCE_COLOR-like: color on, not a TTY
    try:
        result = unified_cli.cmd_queue_list(
            SimpleNamespace(
                workflow_root=None,
                orca_auto_config=None,
                limit=0,
                refresh=False,
                engine=None,
                status=None,
                kind=None,
                json=False,
            )
        )
    finally:
        cli_style.set_color_override(None)
    assert result == 0
    stdout = capsys.readouterr().out
    plain = _strip_ansi(stdout)
    assert "active_simulations:" in plain  # plain layout kept
    assert "orca_auto queue" not in plain  # no summary band
    assert "├─" not in plain and "└─" not in plain  # no tree connectors
    assert "▎" not in plain  # no rail
    assert "\x1b[" in stdout  # color codes are still emitted


def test_watch_uses_discovered_config_for_job_metrics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Regression: no-argument watch must resolve one default config and use it
    # for both activity collection and per-job metrics.
    monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: True)
    monkeypatch.setattr(
        unified_cli, "_queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
    )

    def _list_activities(**kwargs: Any) -> dict[str, Any]:
        seen["list_config"] = kwargs.get("orca_config")
        return _running_job_payload()

    monkeypatch.setattr(unified_cli, "list_activities", _list_activities)
    monkeypatch.setattr(
        cli_common, "_discover_shared_config_path", lambda explicit: "/discovered/orca_auto.yaml"
    )

    seen: dict[str, str | None] = {}

    def _provider(config: str | None) -> dict[str, JobMetrics]:
        seen["config"] = config
        return {}

    def _sleep(_interval: float) -> None:
        raise KeyboardInterrupt

    deps = unified_cli.QueueCliDeps(sleep=_sleep, job_metrics_provider=_provider)
    args = SimpleNamespace(
        action=None,
        orca_auto_config=None,
        workflow_root=None,
        limit=0,
        refresh=False,
        engine=None,
        status=None,
        kind=None,
        json=False,
        watch=True,
        interval=2.0,
    )
    cli_style.set_color_override(True)
    try:
        assert unified_cli.cmd_queue_list(args, deps=deps) == 0
    finally:
        cli_style.set_color_override(None)
    assert seen.get("config") == "/discovered/orca_auto.yaml"  # discovered, not None
    assert seen.get("list_config") == seen.get("config")


def test_default_job_metrics_provider_uses_presentation_independent_sampler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str | None] = []

    class _ProbeLiveSampler:
        def sample(self, config: str | None) -> dict[str, JobMetrics]:
            calls.append(config)
            return {"q": JobMetrics(cpu_percent=None, rss_bytes=1)}

    monkeypatch.setattr(unified_cli, "LiveJobMetricsSampler", _ProbeLiveSampler)
    provider = unified_cli._default_job_metrics_provider()

    assert set(provider("config")) == {"q"}
    assert calls == ["config"]


def test_fmt_rss_units() -> None:
    assert unified_cli._fmt_rss(6 * 1024**3) == "6.0G"
    assert unified_cli._fmt_rss(512 * 1024**2) == "512M"
    assert unified_cli._fmt_rss(700 * 1024) == "700K"


def test_row_job_metric_matches_only_job_rows() -> None:
    metric = JobMetrics(cpu_percent=780.0, rss_bytes=6 * 1024**3)
    job_metrics = {"q1": metric}
    for status in ("running", "retrying", "cancel_requested"):
        assert (
            unified_cli._row_job_metric(
                {"kind": "job", "status": status, "metadata": {"queue_id": "q1"}},
                job_metrics,
            )
            is metric
        )
    # Aliases and terminal/workflow rows cannot bind a live queue metric.
    assert (
        unified_cli._row_job_metric(
            {"kind": "job", "status": "running", "activity_id": "q1"}, job_metrics
        )
        is None
    )
    assert (
        unified_cli._row_job_metric(
            {
                "kind": "job",
                "status": "completed",
                "metadata": {"queue_id": "q1"},
            },
            job_metrics,
        )
        is None
    )
    assert (
        unified_cli._row_job_metric(
            {
                "kind": "workflow",
                "status": "running",
                "metadata": {"queue_id": "q1"},
            },
            job_metrics,
        )
        is None
    )


def test_repair_blocked_is_counted_as_failed() -> None:
    assert unified_cli._summary_status_group("repair_blocked") == "failed"


def test_queue_header_band_respects_terminal_width() -> None:
    rows = [
        (0, {"status": status})
        for status in ("running", "queued", "completed", "repair_blocked", "cancelled", "unknown")
    ]
    cli_style.set_color_override(True)
    try:
        lines = unified_cli._queue_header_band_lines(
            rows,
            active_simulations=3,
            max_width=40,
        )
        narrow_title = unified_cli._queue_header_band_lines(
            rows,
            active_simulations=3,
            max_width=20,
        )[0]
    finally:
        cli_style.set_color_override(None)
    plain_lines = [_strip_ansi(line) for line in lines]
    assert all(terminal_table.display_width(line) <= 40 for line in plain_lines)
    summary = " ".join(plain_lines[1:])
    assert all(
        label in summary for label in ("running", "queued", "done", "failed", "cancelled", "other")
    )
    assert "3 active" in _strip_ansi(narrow_title)


def test_job_annotation_renders_cpu_and_ram() -> None:
    cli_style.set_color_override(True)
    try:
        line = unified_cli._job_annotation(JobMetrics(cpu_percent=780.0, rss_bytes=6 * 1024**3))
    finally:
        cli_style.set_color_override(None)
    plain = _strip_ansi(line)
    assert "cpu 780%" in plain and "ram 6.0G" in plain


def test_job_annotation_omits_cpu_when_none() -> None:
    line = unified_cli._job_annotation(JobMetrics(cpu_percent=None, rss_bytes=2 * 1024**3))
    plain = _strip_ansi(line)
    assert "cpu" not in plain and "ram 2.0G" in plain


def _running_job_payload() -> dict[str, Any]:
    return {
        "count": 1,
        "activities": [
            {
                "activity_id": "q1",
                "kind": "job",
                "engine": "orca",
                "status": "running",
                "label": "TD-DFT",
                "source": "orca_auto_orca",
                "submitted_at": "2026-04-26T02:57:00+00:00",
                "updated_at": "2026-04-26T02:57:00+00:00",
                "metadata": {"queue_id": "q1"},
            },
        ],
        "sources": {},
    }


def test_watch_annotates_running_row_with_job_metrics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: True)
    monkeypatch.setattr(unified_cli, "_queue_terminal_width", lambda: 80)
    monkeypatch.setattr(
        unified_cli, "_queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
    )
    monkeypatch.setattr(unified_cli, "list_activities", lambda **kwargs: _running_job_payload())

    def _sleep(_interval: float) -> None:
        raise KeyboardInterrupt

    def _provider(_config: str | None) -> dict[str, JobMetrics]:
        return {"q1": JobMetrics(cpu_percent=780.0, rss_bytes=6 * 1024**3)}

    deps = unified_cli.QueueCliDeps(sleep=_sleep, job_metrics_provider=_provider)
    args = SimpleNamespace(
        action=None,
        orca_auto_config=None,
        workflow_root=None,
        limit=0,
        refresh=False,
        engine=None,
        status=None,
        kind=None,
        json=False,
        watch=True,
        interval=2.0,
    )
    cli_style.set_color_override(True)
    try:
        assert unified_cli.cmd_queue_list(args, deps=deps) == 0
    finally:
        cli_style.set_color_override(None)
    out = _strip_ansi(capsys.readouterr().out)
    assert "TD-DFT" in out
    assert "cpu 780%" in out and "ram 6.0G" in out
    assert max(terminal_table.display_width(line) for line in out.splitlines()) <= 80


def test_watch_omits_job_metrics_on_force_color_non_tty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        unified_cli, "_queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
    )
    seen: dict[str, object] = {}

    def _list_activities(**kwargs: Any) -> dict[str, Any]:
        seen["child_job_engines"] = kwargs.get("child_job_engines")
        return _running_job_payload()

    monkeypatch.setattr(unified_cli, "list_activities", _list_activities)

    def _sleep(_interval: float) -> None:
        raise KeyboardInterrupt

    calls = {"provider": 0}

    def _provider(_config: str | None) -> dict[str, JobMetrics]:
        calls["provider"] += 1
        return {"q1": JobMetrics(cpu_percent=999.0, rss_bytes=1)}

    deps = unified_cli.QueueCliDeps(sleep=_sleep, job_metrics_provider=_provider)
    args = SimpleNamespace(
        action=None,
        orca_auto_config=None,
        workflow_root=None,
        limit=0,
        refresh=False,
        engine=None,
        status=None,
        kind=None,
        json=False,
        watch=True,
        interval=2.0,
    )
    cli_style.set_color_override(True)
    try:
        assert unified_cli.cmd_queue_list(args, deps=deps) == 0
    finally:
        cli_style.set_color_override(None)
    out = capsys.readouterr().out
    # FORCE_COLOR on a non-TTY still cannot consult the provider or leak metrics.
    assert calls["provider"] == 0
    assert seen["child_job_engines"] == ()
    assert "cpu" not in out and "999%" not in out


def test_watch_annotates_job_metrics_on_no_color_tty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: True)
    monkeypatch.setattr(unified_cli, "_queue_terminal_width", lambda: 80)
    monkeypatch.setattr(
        unified_cli, "_queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
    )
    monkeypatch.setattr(unified_cli, "list_activities", lambda **kwargs: _running_job_payload())

    def _sleep(_interval: float) -> None:
        raise KeyboardInterrupt

    def _provider(_config: str | None) -> dict[str, JobMetrics]:
        return {"q1": JobMetrics(cpu_percent=780.0, rss_bytes=6 * 1024**3)}

    deps = unified_cli.QueueCliDeps(sleep=_sleep, job_metrics_provider=_provider)
    cli_style.set_color_override(False)
    try:
        assert unified_cli.cmd_queue_list(_watch_args(), deps=deps) == 0
    finally:
        cli_style.set_color_override(None)
    out = capsys.readouterr().out
    assert "cpu 780%" in out and "ram 6.0G" in out
    assert "\x1b[2J\x1b[3J\x1b[H" in out
    assert _ANSI_RE.search(out) is None


def test_watch_keeps_job_metrics_on_continuation_when_row_is_narrow(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: True)
    monkeypatch.setattr(unified_cli, "_queue_terminal_width", lambda: 45)
    monkeypatch.setattr(
        unified_cli, "_queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
    )
    monkeypatch.setattr(unified_cli, "list_activities", lambda **kwargs: _running_job_payload())

    def _sleep(_interval: float) -> None:
        raise KeyboardInterrupt

    deps = unified_cli.QueueCliDeps(
        sleep=_sleep,
        job_metrics_provider=lambda _config: {
            "q1": JobMetrics(cpu_percent=780.0, rss_bytes=6 * 1024**3)
        },
    )
    cli_style.set_color_override(False)
    try:
        assert unified_cli.cmd_queue_list(_watch_args(), deps=deps) == 0
    finally:
        cli_style.set_color_override(None)

    out = capsys.readouterr().out
    assert "\n  cpu 780%\n  ram 6.0G\n" in out


def test_default_watch_shows_live_internal_engine_job(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: True)
    monkeypatch.setattr(unified_cli, "_queue_terminal_width", lambda: 100)
    monkeypatch.setattr(
        unified_cli, "_queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
    )
    seen: dict[str, object] = {}

    def _list_activities(**kwargs: Any) -> dict[str, Any]:
        seen["child_job_engines"] = kwargs.get("child_job_engines")
        return {
            "count": 3,
            "activities": [
                {
                    "activity_id": "wf-1",
                    "kind": "workflow",
                    "engine": "workflow",
                    "status": "running",
                    "label": "reaction-workflow",
                    "source": "orca_auto_flow",
                    "submitted_at": "2026-04-26T01:00:00+00:00",
                    "updated_at": "2026-04-26T03:00:00+00:00",
                    "metadata": {"workflow_id": "wf-1"},
                },
                {
                    "activity_id": "xtb-q-1",
                    "kind": "job",
                    "engine": "xtb",
                    "status": "cancel_requested",
                    "label": "path-search",
                    "source": "orca_auto_xtb",
                    "submitted_at": "2026-04-26T02:00:00+00:00",
                    "updated_at": "2026-04-26T03:00:00+00:00",
                    "parent_workflow_id": "wf-1",
                    "metadata": {"queue_id": "xtb-q-1", "workflow_id": "wf-1"},
                },
                {
                    "activity_id": "crest-q-1",
                    "kind": "job",
                    "engine": "crest",
                    "status": "running",
                    "label": "hidden-conformer-search",
                    "source": "orca_auto_crest",
                    "submitted_at": "2026-04-26T02:30:00+00:00",
                    "updated_at": "2026-04-26T03:00:00+00:00",
                    "parent_workflow_id": "wf-1",
                    "metadata": {"queue_id": "crest-q-1", "workflow_id": "wf-1"},
                },
            ],
            "sources": {},
        }

    monkeypatch.setattr(unified_cli, "list_activities", _list_activities)

    def _sleep(_interval: float) -> None:
        raise KeyboardInterrupt

    deps = unified_cli.QueueCliDeps(
        sleep=_sleep,
        job_metrics_provider=lambda _config: {
            "xtb-q-1": JobMetrics(cpu_percent=250.0, rss_bytes=512 * 1024**2)
        },
    )
    cli_style.set_color_override(False)
    try:
        assert unified_cli.cmd_queue_list(_watch_args(), deps=deps) == 0
    finally:
        cli_style.set_color_override(None)

    out = capsys.readouterr().out
    assert seen["child_job_engines"] is None
    assert "path-search" in out
    assert "hidden-conformer-search" not in out
    assert "cpu 250%" in out and "ram 512M" in out


def test_cmd_queue_list_rejects_watch_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = {"emit": 0}

    def _emit_once(args: Any, request: Any) -> int:
        del args, request
        calls["emit"] += 1
        return 0

    args = SimpleNamespace(
        action=None,
        orca_auto_config=None,
        limit=0,
        refresh=False,
        engine=None,
        status=None,
        kind=None,
        json=True,
        watch=True,
        interval=2.0,
    )
    deps = unified_cli.QueueCliDeps(emit_queue_list_once=_emit_once)

    assert unified_cli.cmd_queue_list(args, deps=deps) == 1
    assert calls["emit"] == 0
    err = capsys.readouterr().err
    assert "--watch" in err
    assert "--json" in err


@pytest.fixture(autouse=True)
def _isolate_shared_config_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explicit_shared_config_path(explicit: str | None) -> str | None:
        if not explicit:
            return None
        return str(Path(explicit).expanduser().resolve())

    monkeypatch.setattr(cli_common, "_discover_shared_config_path", _explicit_shared_config_path)
    monkeypatch.setattr(cli_common, "shared_workflow_root_from_config", lambda config_path: None)


def test_queue_elapsed_prefers_attempt_anchor_metadata() -> None:
    now = datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)

    assert (
        unified_cli._queue_elapsed_text(
            {
                "status": "running",
                "submitted_at": "2026-04-26T01:00:00+00:00",
                "updated_at": "2026-04-26T02:00:00+00:00",
                "metadata": {"elapsed_started_at": "2026-04-26T02:45:00+00:00"},
            },
            now=now,
        )
        == "00:15:00"
    )
    assert (
        unified_cli._queue_elapsed_text(
            {
                "status": "completed",
                "submitted_at": "2026-04-26T01:00:00+00:00",
                "updated_at": "2026-04-26T02:20:00+00:00",
                "metadata": {"last_restarted_at": "2026-04-26T02:00:00+00:00"},
            },
            now=now,
        )
        == "00:20:00"
    )


def test_cmd_queue_list_filters_text_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        unified_cli, "_queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
    )
    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: {
            "count": 3,
            "activities": [
                {
                    "activity_id": "wf-1",
                    "kind": "workflow",
                    "engine": "workflow",
                    "status": "running",
                    "label": "wf-1",
                    "source": "orca_auto_flow",
                    "submitted_at": "2026-04-26T01:00:00+00:00",
                    "updated_at": "2026-04-26T01:00:00+00:00",
                },
                {
                    "activity_id": "xtb-q-1",
                    "kind": "job",
                    "engine": "xtb",
                    "status": "running",
                    "label": "rxn-a",
                    "source": "orca_auto_xtb",
                    "submitted_at": "2026-04-26T02:00:00+00:00",
                    "updated_at": "2026-04-26T02:30:00+00:00",
                    "metadata": {"task_kind": "path_search"},
                },
                {
                    "activity_id": "crest-q-1",
                    "kind": "job",
                    "engine": "crest",
                    "status": "pending",
                    "label": "mol-a",
                    "source": "orca_auto_crest",
                    "submitted_at": "2026-04-26T02:15:00+00:00",
                    "updated_at": "2026-04-26T02:15:00+00:00",
                },
            ],
            "sources": {},
        },
    )

    result = unified_cli.cmd_queue_list(
        SimpleNamespace(
            workflow_root=None,
            orca_auto_config=None,
            limit=0,
            refresh=False,
            engine=["xtb"],
            status=["running"],
            kind=["job"],
            json=False,
        )
    )

    assert result == 0
    stdout = capsys.readouterr().out
    assert "active_simulations: 1" in stdout
    assert (
        "Status" in stdout
        and "Name" in stdout
        and "Detail" in stdout
        and "ID" in stdout
        and "Elapsed" in stdout
    )
    assert "▶" in stdout
    assert "xtb-q-1" in stdout
    assert "TS path" in stdout
    assert "01:00:00" in stdout
    assert "crest-q-1" not in stdout
    assert "wf-1" not in stdout


def test_cmd_queue_list_tty_renders_styled_view(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: True)
    monkeypatch.setattr(
        unified_cli, "_queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
    )
    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: {
            "count": 3,
            "activities": [
                {
                    "activity_id": "wf-1",
                    "kind": "workflow",
                    "engine": "workflow",
                    "status": "running",
                    "label": "screen",
                    "source": "orca_auto_flow",
                    "submitted_at": "2026-04-26T00:47:00+00:00",
                    "updated_at": "2026-04-26T00:47:00+00:00",
                },
                {
                    "activity_id": "orca-1",
                    "kind": "job",
                    "engine": "orca",
                    "status": "completed",
                    "label": "opt",
                    "source": "orca_auto_orca",
                    "parent_workflow_id": "wf-1",
                    "metadata": {"workflow_id": "wf-1"},
                    "submitted_at": "2026-04-26T02:00:00+00:00",
                    "updated_at": "2026-04-26T02:41:00+00:00",
                },
                {
                    "activity_id": "orca-2",
                    "kind": "job",
                    "engine": "orca",
                    "status": "running",
                    "label": "freq",
                    "source": "orca_auto_orca",
                    "parent_workflow_id": "wf-1",
                    "metadata": {"workflow_id": "wf-1"},
                    "submitted_at": "2026-04-26T02:57:00+00:00",
                    "updated_at": "2026-04-26T02:57:00+00:00",
                },
            ],
            "sources": {},
        },
    )

    # ``set_color_override`` is process-global, so always restore it.
    cli_style.set_color_override(True)
    try:
        result = unified_cli.cmd_queue_list(
            SimpleNamespace(
                workflow_root=None,
                orca_auto_config=None,
                limit=0,
                refresh=False,
                engine=None,
                status=None,
                kind=None,
                json=False,
            )
        )
    finally:
        cli_style.set_color_override(None)

    assert result == 0
    stdout = capsys.readouterr().out
    plain = _strip_ansi(stdout)

    # The styled summary band replaces the plain ``active_simulations:`` line on
    # a TTY, and reports the status breakdown.
    assert "orca_auto queue" in plain
    assert "active" in plain and "running" in plain
    assert "active_simulations:" not in plain
    # Tree connectors for the workflow's ORCA children plus the per-row rail.
    assert "├─" in plain and "└─" in plain
    assert "▎" in plain
    # Real ANSI SGR codes were emitted (not just the plain fallback).
    assert "\x1b[" in stdout


def test_cmd_queue_list_tty_rail_never_overflows_terminal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from orca_auto.terminal_table import display_width

    monkeypatch.setattr(
        unified_cli, "_queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
    )
    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: {
            "count": 3,
            "activities": [
                {
                    "activity_id": "wf-1",
                    "kind": "workflow",
                    "engine": "workflow",
                    "status": "running",
                    "label": "screen",
                    "source": "orca_auto_flow",
                    "submitted_at": "2026-04-26T00:47:00+00:00",
                    "updated_at": "2026-04-26T00:47:00+00:00",
                },
                {
                    "activity_id": "orca-1",
                    "kind": "job",
                    "engine": "orca",
                    "status": "completed",
                    "label": "opt",
                    "source": "orca_auto_orca",
                    "parent_workflow_id": "wf-1",
                    "metadata": {"workflow_id": "wf-1"},
                    "submitted_at": "2026-04-26T02:00:00+00:00",
                    "updated_at": "2026-04-26T02:41:00+00:00",
                },
                {
                    "activity_id": "orca-2",
                    "kind": "job",
                    "engine": "orca",
                    "status": "running",
                    "label": "freq",
                    "source": "orca_auto_orca",
                    "parent_workflow_id": "wf-1",
                    "metadata": {"workflow_id": "wf-1"},
                    "submitted_at": "2026-04-26T02:57:00+00:00",
                    "updated_at": "2026-04-26T02:57:00+00:00",
                },
            ],
            "sources": {},
        },
    )

    args = SimpleNamespace(
        workflow_root=None,
        orca_auto_config=None,
        limit=0,
        refresh=False,
        engine=None,
        status=None,
        kind=None,
        json=False,
    )

    def _render(width: int, *, color: bool) -> str:
        monkeypatch.setattr(unified_cli, "_queue_terminal_width", lambda: width)
        # Interactive layout needs a real terminal; color alone (e.g. FORCE_COLOR)
        # must not restructure. color=False stays plain via color_enabled anyway.
        monkeypatch.setattr(unified_cli, "_stdout_isatty", lambda: True)
        cli_style.set_color_override(color)
        try:
            assert unified_cli.cmd_queue_list(args) == 0
        finally:
            cli_style.set_color_override(None)
        return capsys.readouterr().out

    def _table_lines(out: str) -> list[str]:
        lines = _strip_ansi(out).split("\n")
        # The table starts at the header row; the band above it is not
        # width-bounded and is excluded.
        header = next(i for i, line in enumerate(lines) if "Status" in line and "Name" in line)
        return [line for line in lines[header:] if line.strip()]

    def _table_width(out: str) -> int:
        # The divider is a run of dashes exactly as wide as the table, before any
        # rail/gutter, so it recovers the intrinsic width regardless of the rail.
        divider = next(line for line in _strip_ansi(out).split("\n") if "─" in line)
        return divider.count("─")

    # Fully shrink the styled table to learn its (tree-glyph) column floor.
    min_width = _table_width(_render(1, color=True))

    # At a terminal exactly as wide as that floor the rail cannot be absorbed, so
    # it must be dropped and the block must not overflow. The original code added
    # the rail unconditionally and overflowed by the rail width here.
    tight = _table_lines(_render(min_width, color=True))
    assert max(display_width(line) for line in tight) <= min_width
    assert not any(line.startswith("▎") for line in tight)

    # With just enough extra room the rail returns and still fits.
    roomy = _table_lines(_render(min_width + 2, color=True))
    assert any(line.startswith("▎") for line in roomy)
    assert max(display_width(line) for line in roomy) <= min_width + 2


def test_cmd_queue_list_hides_non_orca_workflow_children_in_default_text_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        unified_cli, "_queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
    )
    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: {
            "count": 5,
            "activities": [
                {
                    "activity_id": "wf-1",
                    "kind": "workflow",
                    "engine": "workflow",
                    "status": "running",
                    "label": "reaction-case",
                    "source": "orca_auto_flow",
                    "submitted_at": "2026-04-26T01:30:00+00:00",
                    "updated_at": "2026-04-26T02:00:00+00:00",
                    "metadata": {
                        "template_name": "reaction_ts_search",
                        "current_engine": "orca",
                        "request_parameters": {"crest_mode": "nci"},
                    },
                },
                {
                    "activity_id": "xtb-q-1",
                    "kind": "job",
                    "engine": "xtb",
                    "status": "running",
                    "label": "path-search",
                    "source": "orca_auto_xtb",
                    "submitted_at": "2026-04-26T02:00:00+00:00",
                    "updated_at": "2026-04-26T02:15:00+00:00",
                    "metadata": {
                        "task_kind": "path_search",
                        "workflow_id": "wf-1",
                        "job_dir": "/tmp/workflows/wf-1/02_xtb/xtb_path_search_01",
                    },
                },
                {
                    "activity_id": "crest-q-1",
                    "kind": "job",
                    "engine": "crest",
                    "status": "pending",
                    "label": "conformer-search",
                    "source": "orca_auto_crest",
                    "submitted_at": "2026-04-26T02:10:00+00:00",
                    "updated_at": "2026-04-26T02:10:00+00:00",
                    "metadata": {
                        "task_kind": "conformer_search",
                        "workflow_id": "wf-1",
                        "job_dir": "/tmp/workflows/wf-1/01_crest/crest_reactant_01",
                    },
                },
                {
                    "activity_id": "orca-q-1",
                    "kind": "job",
                    "engine": "orca",
                    "status": "running",
                    "label": "ts-opt",
                    "source": "orca_auto_orca",
                    "submitted_at": "2026-04-26T02:00:00+00:00",
                    "updated_at": "2026-04-26T02:20:00+00:00",
                    "metadata": {
                        "task_kind": "optts_freq",
                        "workflow_id": "wf-1",
                        "reaction_dir": "/tmp/workflows/wf-1/03_orca/case_001",
                    },
                },
                {
                    "activity_id": "orca-q-engine-job",
                    "kind": "job",
                    "engine": "orca",
                    "status": "running",
                    "label": "queued-ts",
                    "source": "orca_auto_orca",
                    "submitted_at": "2026-04-26T00:30:00+00:00",
                    "updated_at": "2026-04-26T01:30:00+00:00",
                    "metadata": {
                        "job_type": "neb",
                        "reaction_dir": "/tmp/orca/runs/case_002",
                    },
                },
            ],
            "sources": {},
        },
    )

    result = unified_cli.cmd_queue_list(
        SimpleNamespace(
            workflow_root=None,
            orca_auto_config=None,
            limit=0,
            refresh=False,
            engine=None,
            status=None,
            kind=None,
            json=False,
        )
    )

    assert result == 0
    stdout = capsys.readouterr().out
    assert "active_simulations: 3" in stdout
    assert "▶" in stdout
    assert "wf-1" in stdout
    assert "ts_search(nci)" in stdout
    assert "xtb-q-1" not in stdout
    assert "crest-q-1" not in stdout
    assert "orca-q-1" in stdout
    assert "OptTS+Freq" in stdout
    assert "orca-q-engine-job" in stdout
    assert "NEB" in stdout


def test_cmd_queue_list_shows_all_workflow_child_jobs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        unified_cli, "_queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
    )
    child_rows = [
        {
            "activity_id": f"orca-q-{index}",
            "kind": "job",
            "engine": "orca",
            "status": "running",
            "label": f"ts-{index}",
            "source": "orca_auto_orca",
            "submitted_at": "2026-04-26T02:00:00+00:00",
            "updated_at": "2026-04-26T02:00:00+00:00",
            "metadata": {
                "task_kind": "optts_freq",
                "reaction_dir": f"/tmp/orca/wf-1/03_orca/case_{index:03d}",
            },
        }
        for index in range(1, 10)
    ]
    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: {
            "count": 10,
            "activities": [
                {
                    "activity_id": "wf-1",
                    "kind": "workflow",
                    "engine": "workflow",
                    "status": "running",
                    "label": "reaction-case",
                    "source": "orca_auto_flow",
                    "submitted_at": "2026-04-26T01:00:00+00:00",
                    "updated_at": "2026-04-26T01:00:00+00:00",
                    "metadata": {
                        "template_name": "reaction_ts_search",
                        "current_engine": "orca",
                    },
                },
                *child_rows,
            ],
            "sources": {},
        },
    )

    result = unified_cli.cmd_queue_list(
        SimpleNamespace(
            workflow_root=None,
            orca_auto_config=None,
            limit=0,
            refresh=False,
            engine=None,
            status=None,
            kind=None,
            json=False,
        )
    )

    assert result == 0
    stdout = capsys.readouterr().out
    assert "active_simulations: 9" in stdout
    assert stdout.count("▶") >= 1
    assert stdout.count("orca-q-") == 9
    assert "wf-1" in stdout
    assert "ts_search" in stdout


def test_cmd_queue_list_reports_empty_filtered_results(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: {
            "count": 1,
            "activities": [
                {
                    "activity_id": "wf-1",
                    "kind": "workflow",
                    "engine": "workflow",
                    "status": "running",
                    "label": "reaction-case",
                    "source": "orca_auto_flow",
                    "submitted_at": "2026-04-26T01:00:00+00:00",
                    "updated_at": "2026-04-26T01:00:00+00:00",
                    "metadata": {"template_name": "reaction_ts_search"},
                }
            ],
            "sources": {},
        },
    )

    result = unified_cli.cmd_queue_list(
        SimpleNamespace(
            workflow_root=None,
            orca_auto_config=None,
            limit=0,
            refresh=False,
            engine=["orca"],
            status=["failed"],
            kind=["job"],
            json=False,
        )
    )

    assert result == 0
    stdout = capsys.readouterr().out
    assert "active_simulations: 0" in stdout
    assert "No matching activities." in stdout
    assert "Status" not in stdout


def test_cmd_queue_list_json_filters_payload(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: {
            "count": 2,
            "activities": [
                {
                    "activity_id": "orca-q-1",
                    "kind": "job",
                    "engine": "orca",
                    "status": "running",
                    "label": "ts-1",
                    "source": "orca_auto_orca",
                },
                {
                    "activity_id": "wf-1",
                    "kind": "workflow",
                    "engine": "xtb",
                    "status": "queued",
                    "label": "wf-1",
                    "source": "orca_auto_flow",
                },
            ],
            "sources": {"orca_config": "/tmp/orca_auto.yaml"},
        },
    )

    result = unified_cli.cmd_queue_list(
        SimpleNamespace(
            workflow_root=None,
            orca_auto_config=None,
            limit=0,
            refresh=False,
            engine=["orca"],
            status=None,
            kind=None,
            json=True,
        )
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["active_simulations"] == 1
    assert payload["activities"][0]["activity_id"] == "orca-q-1"
    assert payload["sources"]["orca_config"] == "/tmp/orca_auto.yaml"


def test_cmd_queue_list_uses_global_active_simulation_count_from_full_payload(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: {
            "count": 3,
            "activities": [
                {
                    "activity_id": "xtb-q-1",
                    "kind": "job",
                    "engine": "xtb",
                    "status": "running",
                    "label": "rxn-a",
                    "source": "orca_auto_xtb",
                },
                {
                    "activity_id": "orca-q-1",
                    "kind": "job",
                    "engine": "orca",
                    "status": "running",
                    "label": "ts-a",
                    "source": "orca_auto_orca",
                },
                {
                    "activity_id": "xtb-q-2",
                    "kind": "job",
                    "engine": "xtb",
                    "status": "running",
                    "label": "rxn-b",
                    "source": "orca_auto_xtb",
                },
            ],
            "sources": {"orca_config": "/tmp/orca_auto.yaml"},
        },
    )

    def _fake_count(items: list[dict[str, Any]], *, config_path: str | None = None) -> int:
        captured["items"] = items
        captured["config_path"] = config_path
        return 7

    monkeypatch.setattr(unified_cli, "count_global_active_simulations", _fake_count)

    result = unified_cli.cmd_queue_list(
        SimpleNamespace(
            workflow_root=None,
            orca_auto_config=None,
            limit=1,
            refresh=False,
            engine=["xtb"],
            status=["running"],
            kind=["job"],
            json=True,
        )
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["active_simulations"] == 7
    assert payload["activities"][0]["activity_id"] == "xtb-q-1"
    assert len(captured["items"]) == 3
    assert captured["config_path"] == "/tmp/orca_auto.yaml"


def test_cmd_queue_list_applies_limit_after_filters(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: {
            "count": 4,
            "activities": [
                {
                    "activity_id": "crest-q-1",
                    "kind": "job",
                    "engine": "crest",
                    "status": "pending",
                    "label": "mol-a",
                    "source": "orca_auto_crest",
                },
                {
                    "activity_id": "xtb-q-1",
                    "kind": "job",
                    "engine": "xtb",
                    "status": "running",
                    "label": "rxn-a",
                    "source": "orca_auto_xtb",
                },
                {
                    "activity_id": "orca-q-1",
                    "kind": "job",
                    "engine": "orca",
                    "status": "running",
                    "label": "ts-a",
                    "source": "orca_auto_orca",
                },
                {
                    "activity_id": "xtb-q-2",
                    "kind": "job",
                    "engine": "xtb",
                    "status": "running",
                    "label": "rxn-b",
                    "source": "orca_auto_xtb",
                },
            ],
            "sources": {},
        },
    )

    result = unified_cli.cmd_queue_list(
        SimpleNamespace(
            workflow_root=None,
            orca_auto_config=None,
            limit=1,
            refresh=False,
            engine=["xtb"],
            status=["running"],
            kind=["job"],
            json=True,
        )
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["active_simulations"] == 3
    assert payload["activities"][0]["activity_id"] == "xtb-q-1"


def test_cmd_queue_list_clear_text_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        unified_cli,
        "clear_activities",
        lambda **kwargs: {
            "total_cleared": 5,
            "cleared": {
                "workflows": 1,
                "xtb_queue_entries": 2,
                "crest_queue_entries": 0,
                "orca_queue_entries": 1,
                "orca_run_states": 1,
            },
            "sources": {},
        },
    )

    result = unified_cli.cmd_queue_list(
        SimpleNamespace(
            action="clear",
            workflow_root=None,
            orca_auto_config="/tmp/orca_auto.yaml",
            limit=0,
            refresh=False,
            engine=None,
            status=None,
            kind=None,
            json=False,
        )
    )

    assert result == 0
    stdout = capsys.readouterr().out
    assert "Cleared 5 completed/failed/cancelled entries." in stdout
    assert "workflows: 1" in stdout
    assert "xTB queue entries: 2" in stdout
    assert "ORCA queue entries: 1" in stdout
    assert "ORCA run states: 1" in stdout


def test_cmd_queue_list_clear_json_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        unified_cli,
        "clear_activities",
        lambda **kwargs: {
            "total_cleared": 0,
            "cleared": {
                "workflows": 0,
                "xtb_queue_entries": 0,
                "crest_queue_entries": 0,
                "orca_queue_entries": 0,
                "orca_run_states": 0,
            },
            "sources": {"workflow_root": "/tmp/workflows"},
        },
    )

    result = unified_cli.cmd_queue_list(
        SimpleNamespace(
            action="clear",
            workflow_root=None,
            orca_auto_config="/tmp/orca_auto.yaml",
            limit=0,
            refresh=False,
            engine=None,
            status=None,
            kind=None,
            json=True,
        )
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_cleared"] == 0
    assert payload["sources"]["workflow_root"] == "/tmp/workflows"


def test_cmd_queue_list_clear_rejects_filters(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        unified_cli,
        "clear_activities",
        lambda **kwargs: pytest.fail("clear_activities should not run"),
    )

    result = unified_cli.cmd_queue_list(
        SimpleNamespace(
            action="clear",
            workflow_root=None,
            orca_auto_config="/tmp/orca_auto.yaml",
            limit=0,
            refresh=False,
            engine=["orca"],
            status=None,
            kind=None,
            json=False,
        )
    )

    assert result == 1
    assert (
        capsys.readouterr().err
        == "error: `orca_auto queue list clear` does not support --engine/--status/--kind/--limit filters.\n"
    )


def test_cmd_queue_cancel_reports_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_cancel_activity(**kwargs: Any) -> dict[str, Any]:
        raise LookupError("Activity target not found: missing")

    monkeypatch.setattr(unified_cli, "cancel_activity", fake_cancel_activity)

    result = unified_cli.cmd_queue_cancel(
        SimpleNamespace(
            target="missing",
            workflow_root=None,
            orca_auto_config=None,
            json=False,
        )
    )

    assert result == 1
    assert capsys.readouterr().err == (
        "error: Activity target not found: missing\n"
        "hint: Run `orca_auto queue list` to see valid targets.\n"
    )


def test_cmd_queue_cancel_reports_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_cancel_activity(**kwargs: Any) -> dict[str, Any]:
        raise TimeoutError(
            "Workflow is busy and could not be locked for cancellation within 5s: /tmp/wf_busy"
        )

    monkeypatch.setattr(unified_cli, "cancel_activity", fake_cancel_activity)

    result = unified_cli.cmd_queue_cancel(
        SimpleNamespace(
            target="wf_busy",
            workflow_root=None,
            orca_auto_config=None,
            json=False,
        )
    )

    assert result == 1
    assert capsys.readouterr().err == (
        "error: Workflow is busy and could not be locked for cancellation within 5s: /tmp/wf_busy\n"
        "hint: Run `orca_auto queue list` to see valid targets.\n"
    )


def test_cmd_queue_cancel_json_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        unified_cli,
        "cancel_activity",
        lambda **kwargs: {
            "activity_id": "crest-q-1",
            "kind": "job",
            "engine": "crest",
            "source": "orca_auto_crest",
            "label": "mol-a",
            "status": "cancel_requested",
            "cancel_target": "crest-q-1",
        },
    )

    result = unified_cli.cmd_queue_cancel(
        SimpleNamespace(
            target="crest-q-1",
            workflow_root=None,
            orca_auto_config="/tmp/orca_auto.yaml",
            json=True,
        )
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "cancel_requested"
    assert payload["engine"] == "crest"
