from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from orca_auto import activity_labels, terminal_table
from orca_auto import activity_rendering as rendering


def test_queue_elapsed_uses_attempt_metadata_and_clamps_negative_durations() -> None:
    item = {
        "status": "completed",
        "submitted_at": "2026-05-20T00:00:00+00:00",
        "updated_at": "2026-05-20T00:00:05+00:00",
        "metadata": {"elapsed_started_at": "2026-05-20T00:01:00+00:00"},
    }

    assert (
        activity_labels.queue_elapsed_text(item, now=datetime(2026, 5, 21, tzinfo=UTC))
        == "00:00:00"
    )

    running = {
        "status": "running",
        "updated_at": "2026-05-20T00:00:00+00:00",
        "metadata": {"elapsed_started_at": "2026-05-20T00:00:10Z"},
    }

    assert (
        activity_labels.queue_elapsed_text(
            running,
            now=datetime(2026, 5, 20, 0, 1, 15, tzinfo=UTC),
        )
        == "00:01:05"
    )


def test_queue_name_falls_back_to_label_without_workspace() -> None:
    assert (
        activity_labels.queue_name_text(
            {
                "activity_id": "wf_002",
                "kind": "job",
                "engine": "orca",
                "status": "running",
                "label": "reaction-case",
                "metadata": {},
            }
        )
        == "reaction-case"
    )


def _table_lines(
    rows: Sequence[dict[str, Any]], *, max_width: int | None = None, active: int = 0
) -> list[str]:
    table = rendering.queue_list_table(
        {"activities": rows, "active_simulations": active}, max_width=max_width
    )
    return [table.header, table.divider, *table.rows]


def test_queue_list_table_truncates_wide_unicode_without_column_drift(monkeypatch) -> None:
    monkeypatch.setattr(
        activity_labels,
        "queue_table_now",
        lambda: datetime(2026, 5, 20, 0, 10, 0, tzinfo=UTC),
    )
    rows = [
        {
            "activity_id": "wf_한국어_very_long_identifier",
            "kind": "job",
            "engine": "orca",
            "status": "running",
            "submitted_at": "2026-05-20T00:00:00+00:00",
            "metadata": {
                "selected_inp_name": "opt.inp",
                "reaction_dir": "/tmp/매우긴계산이름_very_long_reaction_name",
            },
        },
        {
            "activity_id": "orca_1",
            "kind": "job",
            "engine": "orca",
            "status": "submitted",
            "updated_at": "2026-05-20T00:00:00+00:00",
            "metadata": {
                "selected_inp_name": "긴파일이름_opt_ts_freq.inp",
            },
        },
    ]

    lines = _table_lines(rows)
    widths = [terminal_table.display_width(line) for line in lines]

    assert len(set(widths)) == 1
    assert "..." in "\n".join(lines)
    assert "매우긴계산이름" in "\n".join(lines)


def _basic_rows() -> list[dict[str, object]]:
    return [
        {
            "activity_id": "orca_a_very_long_activity_identifier_value",
            "kind": "job",
            "engine": "orca",
            "status": "running",
            "label": "a_reasonably_long_reaction_name_here",
            "updated_at": "2026-05-20T00:00:00+00:00",
            "metadata": {"job_type": "opt"},
        }
    ]


def test_queue_list_table_keeps_one_row_line_per_activity(monkeypatch) -> None:
    monkeypatch.setattr(
        activity_labels,
        "queue_table_now",
        lambda: datetime(2026, 5, 20, 0, 10, 0, tzinfo=UTC),
    )
    rows = [*_basic_rows(), {**_basic_rows()[0], "activity_id": "orca_b", "status": "failed"}]

    table = rendering.queue_list_table(
        {"activities": rows, "active_simulations": 3}, max_width=None
    )

    assert table.summary == "active_simulations: 3"
    assert table.activities == tuple(rows)
    assert len(table.rows) == 2
    assert table.rows[1].startswith("❌")
    assert all(name in table.header for name in ("Status", "Name", "Detail", "ID", "Elapsed"))
    assert table.notes == ()


def test_queue_list_table_without_rows_keeps_the_blocker_notes() -> None:
    blocker = {
        "queue_id": "*",
        "allowed_root": "/runs",
        "scope": "admission_store",
        "reason": "Admission slot file is not valid JSON",
        "next_action": "Repair it.",
    }

    table = rendering.queue_list_table(
        {"activities": [], "active_simulations": 0, "admission_blockers": [blocker]},
        max_width=80,
    )

    assert (table.header, table.divider, table.rows) == ("", "", ())
    assert table.notes == (
        "admission_blocked: ORCA queue /runs (queue_id=*)",
        "  Admission slot file is not valid JSON",
        "  Repair it.",
    )


def test_queue_list_table_fits_within_max_width(monkeypatch) -> None:
    monkeypatch.setattr(
        activity_labels,
        "queue_table_now",
        lambda: datetime(2026, 5, 20, 0, 10, 0, tzinfo=UTC),
    )

    lines = _table_lines(_basic_rows(), max_width=50)
    widths = [terminal_table.display_width(line) for line in lines]

    assert len(set(widths)) == 1
    assert widths[0] <= 50


def test_queue_list_table_shrinks_detail_before_id(monkeypatch) -> None:
    monkeypatch.setattr(
        activity_labels,
        "queue_table_now",
        lambda: datetime(2026, 5, 20, 0, 10, 0, tzinfo=UTC),
    )

    rows = [
        {
            "activity_id": "orca_keep_this_id",
            "kind": "job",
            "engine": "orca",
            "status": "running",
            "label": "a_really_really_long_reaction_name_value_here",
            "updated_at": "2026-05-20T00:00:00+00:00",
            "metadata": {"job_type": "opt"},
        }
    ]

    # Tight enough to force the name column to shrink, but the ID — which doubles
    # as the `queue cancel` target — is the last column to give up space.
    lines = _table_lines(rows, max_width=60)

    assert "orca_keep_this_id" in "\n".join(lines)


def test_queue_list_table_keeps_elapsed_of_one_hundred_hours_or_more(monkeypatch) -> None:
    rows = [
        {
            "activity_id": "orca_long_job",
            "kind": "job",
            "engine": "orca",
            "status": "running",
            "label": "multi_day_freq",
            "updated_at": "2026-05-15T00:00:00+00:00",
            "metadata": {"elapsed_started_at": "2026-05-15T00:00:00+00:00"},
        }
    ]
    monkeypatch.setattr(
        activity_labels, "queue_table_now", lambda: datetime(2026, 5, 20, 12, 5, 7, tzinfo=UTC)
    )

    for max_width in (None, 50):
        lines = _table_lines(rows, max_width=max_width)
        widths = [terminal_table.display_width(line) for line in lines]

        assert lines[2].endswith("132:05:07")
        assert len(set(widths)) == 1


def test_terminal_max_width_returns_none_without_terminal(monkeypatch) -> None:
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(
        terminal_table.shutil,
        "get_terminal_size",
        lambda fallback=(0, 0): __import__("os").terminal_size((0, 0)),
    )

    assert terminal_table.terminal_max_width() is None


def test_queue_worker_log_lines_name_running_and_failed_rows_only() -> None:
    from orca_auto import activity_rendering

    rows = [
        {"activity_id": "q-run", "status": "running", "worker_log": "/runs/logs/q-run.log"},
        {"activity_id": "q-rb", "status": "repair_blocked", "worker_log": "/l/q-rb.log"},
        {"activity_id": "q-done", "status": "completed", "worker_log": "/l/q-done.log"},
        {"activity_id": "q-nolog", "status": "running", "worker_log": ""},
        {"activity_id": "q-legacy", "status": "failed"},
    ]

    assert activity_rendering.queue_worker_log_lines(rows) == [
        "worker_log: q-run /runs/logs/q-run.log",
        "worker_log: q-rb /l/q-rb.log",
    ]
    assert activity_rendering.queue_worker_log_lines([]) == []


def test_queue_clear_lines_report_removed_worker_logs() -> None:
    from orca_auto import activity_rendering

    lines = activity_rendering.queue_clear_lines(
        {
            "total_cleared": 2,
            "cleared": {"orca_queue_entries": 2, "orca_run_states": 0},
            "removed_worker_logs": 1,
        }
    )

    assert lines == [
        "Cleared 2 completed/failed/cancelled entries.",
        "  ORCA queue entries: 2",
        "  worker logs removed: 1",
    ]
    assert activity_rendering.queue_clear_lines({"total_cleared": 0}) == ["Nothing to clear."]
