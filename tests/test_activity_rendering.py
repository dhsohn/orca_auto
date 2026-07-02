from __future__ import annotations

from datetime import UTC, datetime

from orca_auto import activity_rendering as rendering


def test_queue_elapsed_uses_restart_metadata_and_clamps_negative_durations() -> None:
    item = {
        "status": "completed",
        "submitted_at": "2026-05-20T00:00:00+00:00",
        "updated_at": "2026-05-20T00:00:05+00:00",
        "metadata": {"restart_summary": {"restarted_at": "2026-05-20T00:01:00+00:00"}},
    }

    assert rendering._queue_elapsed_text(item) == "00:00:00"

    running = {
        "status": "running",
        "updated_at": "2026-05-20T00:00:00+00:00",
        "metadata": {"last_restarted_at": "2026-05-20T00:00:10Z"},
    }

    assert (
        rendering._queue_elapsed_text(
            running,
            now=datetime(2026, 5, 20, 0, 1, 15, tzinfo=UTC),
        )
        == "00:01:05"
    )


def test_queue_name_uses_workflow_workspace_for_generic_input_label() -> None:
    assert (
        rendering._queue_name_text(
            {
                "activity_id": "wf_006",
                "kind": "workflow",
                "status": "running",
                "label": "input",
                "metadata": {"workspace_dir": "/tmp/workflow_runs/wf_006"},
            }
        )
        == "wf_006"
    )


def test_queue_name_keeps_meaningful_workflow_label() -> None:
    assert (
        rendering._queue_name_text(
            {
                "activity_id": "wf_001",
                "kind": "workflow",
                "status": "running",
                "label": "reaction-case",
                "metadata": {"workspace_dir": "/tmp/workflow_runs/wf_001"},
            }
        )
        == "reaction-case"
    )


def test_queue_table_lines_truncates_wide_unicode_without_column_drift(monkeypatch) -> None:
    monkeypatch.setattr(
        rendering,
        "_queue_table_now",
        lambda: datetime(2026, 5, 20, 0, 10, 0, tzinfo=UTC),
    )
    rows = [
        (
            0,
            {
                "activity_id": "wf_한국어_very_long_identifier",
                "kind": "workflow",
                "status": "running",
                "submitted_at": "2026-05-20T00:00:00+00:00",
                "metadata": {
                    "template_name": "reaction_ts_search",
                    "workspace_dir": "/tmp/매우긴워크플로우이름_very_long_workflow_name",
                    "request_parameters": {"crest_mode": "nci"},
                },
            },
        ),
        (
            1,
            {
                "activity_id": "orca_1",
                "kind": "job",
                "engine": "orca",
                "status": "submitted",
                "updated_at": "2026-05-20T00:00:00+00:00",
                "metadata": {
                    "workflow_id": "wf_한국어_very_long_identifier",
                    "selected_inp_name": "긴파일이름_opt_ts_freq.inp",
                },
            },
        ),
    ]

    lines = rendering.queue_table_lines(rows)
    widths = [rendering._queue_display_width(line) for line in lines]

    assert len(set(widths)) == 1
    assert "..." in "\n".join(lines)
    assert "매우긴워크플로우" in "\n".join(lines)


def _basic_rows() -> list[tuple[int, dict[str, object]]]:
    return [
        (
            0,
            {
                "activity_id": "orca_a_very_long_activity_identifier_value",
                "kind": "job",
                "engine": "orca",
                "status": "running",
                "label": "a_reasonably_long_reaction_name_here",
                "updated_at": "2026-05-20T00:00:00+00:00",
                "metadata": {"job_type": "opt"},
            },
        )
    ]


def test_queue_table_lines_omits_id_column_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        rendering,
        "_queue_table_now",
        lambda: datetime(2026, 5, 20, 0, 10, 0, tzinfo=UTC),
    )

    lines = rendering.queue_table_lines(_basic_rows(), include_id=False)
    joined = "\n".join(lines)
    widths = [rendering._queue_display_width(line) for line in lines]

    assert len(set(widths)) == 1
    assert "ID" not in lines[0]
    assert "orca_a_very_long_activity_identifier_value" not in joined
    # Other columns still render.
    assert "Status" in lines[0] and "Name" in lines[0] and "Elapsed" in lines[0]


def test_queue_table_lines_fits_within_max_width(monkeypatch) -> None:
    monkeypatch.setattr(
        rendering,
        "_queue_table_now",
        lambda: datetime(2026, 5, 20, 0, 10, 0, tzinfo=UTC),
    )

    lines = rendering.queue_table_lines(_basic_rows(), max_width=50)
    widths = [rendering._queue_display_width(line) for line in lines]

    assert len(set(widths)) == 1
    assert widths[0] <= 50


def test_queue_table_lines_shrinks_detail_before_id(monkeypatch) -> None:
    monkeypatch.setattr(
        rendering,
        "_queue_table_now",
        lambda: datetime(2026, 5, 20, 0, 10, 0, tzinfo=UTC),
    )

    rows = [
        (
            0,
            {
                "activity_id": "orca_keep_this_id",
                "kind": "job",
                "engine": "orca",
                "status": "running",
                "label": "a_really_really_long_reaction_name_value_here",
                "updated_at": "2026-05-20T00:00:00+00:00",
                "metadata": {"job_type": "opt"},
            },
        )
    ]

    # Tight enough to force the name column to shrink, but the ID — which doubles
    # as the `queue cancel` target — is the last column to give up space.
    lines = rendering.queue_table_lines(rows, max_width=60)

    assert "orca_keep_this_id" in "\n".join(lines)


def test_terminal_max_width_returns_none_without_terminal(monkeypatch) -> None:
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(
        rendering.shutil,
        "get_terminal_size",
        lambda fallback=(0, 0): __import__("os").terminal_size((0, 0)),
    )

    assert rendering._terminal_max_width() is None
