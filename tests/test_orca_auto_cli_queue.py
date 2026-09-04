from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from orca_auto import activity_labels, cli_common, cli_style, terminal_table
from orca_auto import cli_queue as unified_cli
from orca_auto.core.queue import QueueStoreCorruptError

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


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
        activity_labels, "queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
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
        activity_labels.queue_elapsed_text(
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
        activity_labels.queue_elapsed_text(
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
        activity_labels, "queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
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
        activity_labels, "queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
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
        activity_labels, "queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
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
        monkeypatch.setattr(terminal_table, "terminal_max_width", lambda: width)
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


def test_cmd_queue_list_shows_all_workflow_children_in_default_text_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        activity_labels, "queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
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
    assert "xtb-q-1" in stdout
    assert "TS path" in stdout
    assert "crest-q-1" in stdout
    assert "conformer_search" in stdout
    assert "orca-q-1" in stdout
    assert "OptTS+Freq" in stdout
    assert "orca-q-engine-job" in stdout
    assert "NEB" in stdout


def test_cmd_queue_list_shows_all_workflow_child_jobs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        activity_labels, "queue_table_now", lambda: datetime(2026, 4, 26, 3, 0, 0, tzinfo=UTC)
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


def test_cmd_queue_list_clear_rejects_negative_limit_fail_closed(
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
            limit=-1,
            refresh=False,
            engine=None,
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


@pytest.mark.parametrize(
    ("action", "failure"),
    [
        (None, FileNotFoundError("configured file is missing")),
        (None, yaml.YAMLError("configuration YAML is invalid")),
        ("clear", QueueStoreCorruptError("queue store is invalid")),
    ],
)
def test_cmd_queue_list_reports_expected_config_and_store_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str | None,
    failure: Exception,
) -> None:
    monkeypatch.setattr(
        unified_cli,
        "list_activities" if action is None else "clear_activities",
        lambda **_kwargs: (_ for _ in ()).throw(failure),
    )

    result = unified_cli.cmd_queue_list(
        SimpleNamespace(
            action=action,
            workflow_root=None,
            orca_auto_config="/tmp/missing-or-corrupt.yaml",
            limit=0,
            refresh=False,
            engine=None,
            status=None,
            kind=None,
            json=True,
        )
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "hint: Check the config path" in captured.err
    assert "Traceback" not in captured.err


def test_cmd_queue_list_treats_closed_output_pipe_separately_from_state_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **_kwargs: {"activities": [], "sources": {}},
    )
    monkeypatch.setattr(
        unified_cli,
        "count_global_active_simulations",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        unified_cli,
        "_print_queue_list_text",
        lambda **_kwargs: (_ for _ in ()).throw(BrokenPipeError("downstream closed")),
    )

    result = unified_cli.cmd_queue_list(
        SimpleNamespace(
            action=None,
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

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == ""
    assert captured.err == ""


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
        "hint: Check the configured runtime state, then run `orca_auto queue list` "
        "to see valid targets.\n"
    )


def test_cmd_queue_cancel_treats_closed_pipe_as_success_after_durable_cancel(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cancel_calls: list[dict[str, Any]] = []

    def fake_cancel_activity(**kwargs: Any) -> dict[str, str]:
        cancel_calls.append(kwargs)
        return {"activity_id": "job-1"}

    monkeypatch.setattr(unified_cli, "cancel_activity", fake_cancel_activity)
    monkeypatch.setattr(
        unified_cli,
        "_emit_queue_cancel",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BrokenPipeError("downstream closed")),
    )

    result = unified_cli.cmd_queue_cancel(
        SimpleNamespace(
            target="job-1",
            workflow_root=None,
            orca_auto_config=None,
            json=True,
        )
    )

    captured = capsys.readouterr()
    assert result == 0
    assert len(cancel_calls) == 1
    assert captured.out == ""
    assert captured.err == ""


def test_cli_main_silences_closed_pipe_before_interpreter_shutdown() -> None:
    script = """
from types import SimpleNamespace

from orca_auto import cli


class FakeParser:
    def parse_args(self, _argv):
        return SimpleNamespace(no_color=False, func=emit)

    def print_help(self):
        raise AssertionError("unexpected help")


def emit(_args):
    try:
        for _ in range(4096):
            print("x" * 4096)
    except BrokenPipeError:
        return 0
    return 0


cli.build_parser = FakeParser
raise SystemExit(cli.main([]))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    assert process.stdout.readline().startswith("x")
    process.stdout.close()
    stderr = process.stderr.read()
    return_code = process.wait(timeout=10)

    assert return_code == 0
    assert stderr == ""


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
        "hint: Check the configured runtime state, then run `orca_auto queue list` "
        "to see valid targets.\n"
    )


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("configured file is missing"),
        yaml.YAMLError("configuration YAML is invalid"),
        QueueStoreCorruptError("queue store is invalid"),
    ],
)
def test_cmd_queue_cancel_reports_expected_state_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    monkeypatch.setattr(
        unified_cli,
        "cancel_activity",
        lambda **_kwargs: (_ for _ in ()).throw(failure),
    )

    result = unified_cli.cmd_queue_cancel(
        SimpleNamespace(
            target="anything",
            workflow_root="/tmp/workflows",
            orca_auto_config="/tmp/missing-or-corrupt.yaml",
            json=True,
        )
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "hint: Check the configured runtime state" in captured.err
    assert "Traceback" not in captured.err


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


def test_cmd_queue_list_reports_a_missing_runs_root_instead_of_an_empty_queue(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        unified_cli,
        "_workflow_root_for_args",
        lambda args, config_path=None: str(tmp_path / "does_not_exist_root"),
    )
    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: pytest.fail("a missing runs_root must not be listed"),
    )

    result = unified_cli.cmd_queue_list(
        SimpleNamespace(
            action=None,
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

    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "runs_root does not exist" in captured.err
    assert "does_not_exist_root" in captured.err


def test_cmd_queue_list_json_reports_undrained_cancel_transitions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # End to end over the real registry: a cancel that died before journaling
    # leaves an unclearable row, and `queue list --json` is where an operator
    # finds out why.
    from orca_auto.flow import activity, registry
    from orca_auto.flow.state import write_workflow_payload

    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")

    workflow_root = tmp_path / "workflow_runs"
    workspace = workflow_root / "wf-cancel-pending"
    workspace.mkdir(parents=True)
    payload = {
        "workflow_id": "wf-cancel-pending",
        "template_name": "reaction_ts_search",
        "status": "cancelled",
        "requested_at": "2026-08-11T05:00:00+00:00",
        "stages": [],
        "metadata": {
            "cancellation_status_transitions": [
                {"event_id": "wf_evt_1", "status": "cancelled"},
            ]
        },
    }
    write_workflow_payload(workspace, payload)
    registry.sync_workflow_registry(workflow_root, workspace, payload)

    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: activity.list_activities(
            workflow_root=workflow_root,
            shared_config=str(config_path),
        ),
    )

    result = unified_cli.cmd_queue_list(
        SimpleNamespace(
            action=None,
            workflow_root=str(workflow_root),
            orca_auto_config=str(config_path),
            limit=0,
            refresh=False,
            engine=None,
            status=None,
            kind=None,
            json=True,
        )
    )

    assert result == 0
    rows = json.loads(capsys.readouterr().out)["activities"]
    assert [row["activity_id"] for row in rows] == ["wf-cancel-pending"]
    assert rows[0]["metadata"]["cancel_transitions_pending"] == 1


#: One transition in the shape `_stored_cancellation_transitions` accepts.
_STORED_CANCEL_TRANSITION = {
    "event_id": "wf_evt_1",
    "occurred_at": "2026-08-11T05:10:00+00:00",
    "previous_status": "running",
    "status": "cancelled",
}


def _cancel_authority_workflow_root(
    tmp_path: Path,
    *,
    workflow_id: str,
    transitions: list[dict[str, str]],
) -> tuple[Path, Path, Path, dict[str, Any]]:
    """A real workflow root with one cancelled workflow and its registry row.

    The row is synced from the payload as it stands here, so a later payload
    rewrite without a sync leaves exactly the cached count a crashed cancel
    plus a worker drain leaves behind.
    """
    from orca_auto.flow import registry
    from orca_auto.flow.state import write_workflow_payload

    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")

    workflow_root = tmp_path / "workflow_runs"
    workspace = workflow_root / workflow_id
    workspace.mkdir(parents=True)
    payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "template_name": "reaction_ts_search",
        "status": "cancelled",
        "requested_at": "2026-08-11T05:00:00+00:00",
        "stages": [],
        "metadata": {"cancellation_status_transitions": list(transitions)},
    }
    write_workflow_payload(workspace, payload)
    registry.sync_workflow_registry(workflow_root, workspace, payload)
    return workflow_root, workspace, config_path, payload


def _cancel_authority_args(
    workflow_root: Path, config_path: Path, *, as_json: bool
) -> SimpleNamespace:
    # `refresh=False` is the listing an operator gets by default; a refresh
    # reindexes the registry from the payloads and would hide a stale row.
    return SimpleNamespace(
        action=None,
        workflow_root=str(workflow_root),
        orca_auto_config=str(config_path),
        limit=0,
        refresh=False,
        engine=None,
        status=None,
        kind=None,
        json=as_json,
    )


def _patch_real_queue_listing(
    monkeypatch: pytest.MonkeyPatch, workflow_root: Path, config_path: Path
) -> None:
    from orca_auto.flow import activity

    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: activity.list_activities(
            workflow_root=workflow_root,
            shared_config=str(config_path),
        ),
    )


def test_cmd_queue_list_drops_cancel_pending_once_the_payload_is_drained(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # End to end over the real registry: the row was synced while a crashed
    # cancel's transitions were stored, then a worker drained them and rewrote
    # only `workflow.json`. A terminal workflow is skipped without a registry
    # sync, so the row still caches the count. `queue list clear` reads the
    # payload and clears this row, so the listing must stop naming it.
    from orca_auto.flow import registry
    from orca_auto.flow.state import write_workflow_payload

    workflow_root, workspace, config_path, payload = _cancel_authority_workflow_root(
        tmp_path,
        workflow_id="wf-cancel-drained",
        transitions=[dict(_STORED_CANCEL_TRANSITION)],
    )
    payload["metadata"]["cancellation_status_transitions"] = []
    write_workflow_payload(workspace, payload)
    stale_row = registry.list_workflow_registry(workflow_root)[0]
    assert stale_row.metadata["cancel_transitions_pending"] == 1

    _patch_real_queue_listing(monkeypatch, workflow_root, config_path)

    assert (
        unified_cli.cmd_queue_list(_cancel_authority_args(workflow_root, config_path, as_json=True))
        == 0
    )
    rows = json.loads(capsys.readouterr().out)["activities"]
    assert [row["activity_id"] for row in rows] == ["wf-cancel-drained"]
    assert "cancel_transitions_pending" not in rows[0]["metadata"]

    assert (
        unified_cli.cmd_queue_list(
            _cancel_authority_args(workflow_root, config_path, as_json=False)
        )
        == 0
    )
    assert "cancel_pending" not in _strip_ansi(capsys.readouterr().out)

    # The listing and the authoritative guard now agree: this row does clear.
    assert registry.clear_terminal_workflow_registry(workflow_root) == 1


def test_cmd_queue_list_names_a_stored_cancel_transition_the_guard_refuses(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # The other half of the same authority: a transition the payload really
    # still stores is reported and named, and the clear guard really refuses.
    from orca_auto.flow import registry

    workflow_root, _workspace, config_path, _payload = _cancel_authority_workflow_root(
        tmp_path,
        workflow_id="wf-cancel-stored",
        transitions=[dict(_STORED_CANCEL_TRANSITION)],
    )
    _patch_real_queue_listing(monkeypatch, workflow_root, config_path)

    assert (
        unified_cli.cmd_queue_list(_cancel_authority_args(workflow_root, config_path, as_json=True))
        == 0
    )
    rows = json.loads(capsys.readouterr().out)["activities"]
    assert rows[0]["metadata"]["cancel_transitions_pending"] == 1

    assert (
        unified_cli.cmd_queue_list(
            _cancel_authority_args(workflow_root, config_path, as_json=False)
        )
        == 0
    )
    lines = _strip_ansi(capsys.readouterr().out).splitlines()
    assert lines[-2:] == [
        "cancel_pending: wf-cancel-stored=1",
        "  undrained cancel transitions; `queue list clear` refuses these rows.",
    ]

    assert registry.clear_terminal_workflow_registry(workflow_root) == 0


def test_cmd_queue_list_stays_quiet_for_the_normal_empty_transition_list(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # A cancel that completed leaves the key present but empty. Flagging its
    # presence rather than its length would mark every cancelled workflow.
    from orca_auto.flow import registry

    workflow_root, _workspace, config_path, _payload = _cancel_authority_workflow_root(
        tmp_path,
        workflow_id="wf-cancel-normal",
        transitions=[],
    )
    assert (
        "cancel_transitions_pending"
        not in registry.list_workflow_registry(workflow_root)[0].metadata
    )
    _patch_real_queue_listing(monkeypatch, workflow_root, config_path)

    assert (
        unified_cli.cmd_queue_list(_cancel_authority_args(workflow_root, config_path, as_json=True))
        == 0
    )
    rows = json.loads(capsys.readouterr().out)["activities"]
    assert "cancel_transitions_pending" not in rows[0]["metadata"]

    assert (
        unified_cli.cmd_queue_list(
            _cancel_authority_args(workflow_root, config_path, as_json=False)
        )
        == 0
    )
    assert "cancel_pending" not in _strip_ansi(capsys.readouterr().out)


def test_cmd_queue_list_keeps_the_cached_count_when_the_payload_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # Decided behaviour for an unreadable payload: no summary is produced for
    # this workspace, so the cached count is reported. The clear guard refuses
    # the row as well -- on the corruption ground rather than this one -- so
    # the note is not contradicted, and it is the only evidence an operator
    # has left that a cancel was still holding transitions.
    from orca_auto.flow import registry

    workflow_root, workspace, config_path, _payload = _cancel_authority_workflow_root(
        tmp_path,
        workflow_id="wf-cancel-unreadable",
        transitions=[dict(_STORED_CANCEL_TRANSITION)],
    )
    (workspace / "workflow.json").write_text("{ not json", encoding="utf-8")
    _patch_real_queue_listing(monkeypatch, workflow_root, config_path)

    assert (
        unified_cli.cmd_queue_list(_cancel_authority_args(workflow_root, config_path, as_json=True))
        == 0
    )
    rows = json.loads(capsys.readouterr().out)["activities"]
    assert rows[0]["metadata"]["cancel_transitions_pending"] == 1

    assert (
        unified_cli.cmd_queue_list(
            _cancel_authority_args(workflow_root, config_path, as_json=False)
        )
        == 0
    )
    assert "cancel_pending: wf-cancel-unreadable=1" in _strip_ansi(capsys.readouterr().out)

    assert registry.clear_terminal_workflow_registry(workflow_root) == 0


def test_cmd_queue_list_names_a_transition_a_quarantined_twin_cannot_answer_for(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # A second workspace persists the first one's `workflow_id` -- an operator
    # copy of a workspace directory, which `advance` quarantines without
    # rewriting the durable id -- and its directory name sorts first, so the
    # reverse-name workspace scan visits it last. Matched by id it would be the
    # summary the real row is answered from, and its own drained transition
    # list would convert that row's count to zero. The clear guard reads the
    # workspace each row names and still refuses both, so the note must stay.
    from orca_auto.flow import registry
    from orca_auto.flow.state import write_workflow_payload

    workflow_root, _workspace, config_path, _payload = _cancel_authority_workflow_root(
        tmp_path,
        workflow_id="wf-cancel-real",
        transitions=[dict(_STORED_CANCEL_TRANSITION)],
    )
    twin_workspace = workflow_root / "wf-cancel-copy"
    twin_workspace.mkdir()
    twin_payload: dict[str, Any] = {
        "workflow_id": "wf-cancel-real",
        "template_name": "reaction_ts_search",
        "status": "failed",
        "requested_at": "2026-08-11T05:00:00+00:00",
        "stages": [],
        "metadata": {
            "cancellation_status_transitions": [],
            "workflow_error": {
                "status": "failed",
                "scope": "workflow_identity_validation",
                "reason": "workflow directory name does not match persisted workflow_id",
            },
        },
    }
    write_workflow_payload(twin_workspace, twin_payload)
    registry.sync_workflow_registry(workflow_root, twin_workspace, twin_payload)
    twin_row = next(
        row
        for row in registry.list_workflow_registry(workflow_root)
        if row.workflow_id == "wf-cancel-copy"
    )
    assert twin_row.metadata["quarantined_persisted_workflow_id"] == "wf-cancel-real"

    _patch_real_queue_listing(monkeypatch, workflow_root, config_path)

    assert (
        unified_cli.cmd_queue_list(_cancel_authority_args(workflow_root, config_path, as_json=True))
        == 0
    )
    rows = {row["activity_id"]: row for row in json.loads(capsys.readouterr().out)["activities"]}
    assert sorted(rows) == ["wf-cancel-copy", "wf-cancel-real"]
    assert rows["wf-cancel-real"]["metadata"]["cancel_transitions_pending"] == 1
    assert "cancel_transitions_pending" not in rows["wf-cancel-copy"]["metadata"]

    assert (
        unified_cli.cmd_queue_list(
            _cancel_authority_args(workflow_root, config_path, as_json=False)
        )
        == 0
    )
    lines = _strip_ansi(capsys.readouterr().out).splitlines()
    assert lines[-2:] == [
        "cancel_pending: wf-cancel-real=1",
        "  undrained cancel transitions; `queue list clear` refuses these rows.",
    ]

    assert registry.clear_terminal_workflow_registry(workflow_root) == 0


_CANCEL_PENDING_ACTIVITY_ID = "wf_conformer_20260423_082755_542a9e"


def _cancel_pending_queue_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "count": 1,
        "activities": [
            {
                "activity_id": _CANCEL_PENDING_ACTIVITY_ID,
                "kind": "workflow",
                "engine": "workflow",
                "status": "cancelled",
                "label": "rxn-9",
                "source": "orca_auto_flow",
                "submitted_at": "2026-08-11T05:00:00+00:00",
                "updated_at": "2026-08-11T05:20:00+00:00",
                "metadata": metadata,
            }
        ],
        "sources": {},
    }


def _cancel_pending_queue_args() -> SimpleNamespace:
    return SimpleNamespace(
        action=None,
        workflow_root=None,
        orca_auto_config=None,
        limit=0,
        refresh=False,
        engine=None,
        status=None,
        kind=None,
        json=False,
    )


def test_cmd_queue_list_text_names_undrained_cancel_transitions_at_a_real_width(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An operator's terminal is 80 to 120 columns wide. `detail` is soft-capped
    # at 36 columns and surrenders width first, so a marker written into that
    # cell is truncated away exactly where an operator would read it; this row
    # uses the widest workflow label plus a crest mode to force that. The note
    # goes under the table, where no column shrinking reaches it.
    monkeypatch.setattr(terminal_table, "terminal_max_width", lambda: 80)
    monkeypatch.setattr(
        activity_labels, "queue_table_now", lambda: datetime(2026, 8, 11, 6, 0, 0, tzinfo=UTC)
    )
    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: _cancel_pending_queue_payload(
            {
                "template_name": "conformer_screening",
                "request_parameters": {"crest_mode": "quick"},
                "cancel_transitions_pending": 2,
            }
        ),
    )

    assert unified_cli.cmd_queue_list(_cancel_pending_queue_args()) == 0

    lines = _strip_ansi(capsys.readouterr().out).splitlines()
    assert lines[0].startswith("active_simulations:")
    assert "Detail" in lines[1]
    # The row itself is fitted to the terminal and its Detail cell is cut; the
    # note below it is not part of the table and survives intact.
    assert terminal_table.display_width(lines[3]) <= 80
    assert "cancel_pending" not in lines[3]
    assert lines[4:] == [
        f"cancel_pending: {_CANCEL_PENDING_ACTIVITY_ID}=2",
        "  undrained cancel transitions; `queue list clear` refuses these rows.",
    ]


def test_cmd_queue_list_text_stays_quiet_without_undrained_cancel_transitions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Every queue without a stalled cancel keeps the output it had before.
    monkeypatch.setattr(terminal_table, "terminal_max_width", lambda: 80)
    monkeypatch.setattr(
        activity_labels, "queue_table_now", lambda: datetime(2026, 8, 11, 6, 0, 0, tzinfo=UTC)
    )
    monkeypatch.setattr(
        unified_cli,
        "list_activities",
        lambda **kwargs: _cancel_pending_queue_payload({"template_name": "conformer_screening"}),
    )

    assert unified_cli.cmd_queue_list(_cancel_pending_queue_args()) == 0

    assert "cancel_pending" not in _strip_ansi(capsys.readouterr().out)
