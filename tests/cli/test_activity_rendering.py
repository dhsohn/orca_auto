from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from orca_auto import activity_labels, terminal_table
from orca_auto import activity_rendering as rendering

# The confirmed defect: a method-only implicit single point bound in a
# generation of a job directory whose name carries IRC and TS words.
_IRC_NAMED_JOB = "/home/u/runs/MeOPh_OH_TSD_IRC_F_sp_continuous_01"
_SP_IN_IRC_JOB = f"{_IRC_NAMED_JOB}/20261001-000000-0123abcd/sp.inp"


def _run_inp(**metadata: Any) -> dict[str, Any]:
    return {"task_kind": "orca_run_inp", **metadata}


@pytest.mark.parametrize(
    "metadata",
    [
        # Legacy rows: job_type "other" and the full bound path, no detail kind.
        # Neither the file name nor its directories verify what the input runs.
        _run_inp(job_type="other", selected_inp=_SP_IN_IRC_JOB),
        _run_inp(
            job_type="other",
            selected_inp=r"C:\runs\MeOPh_IRC_TS_scan\20261001-000000-0123abcd\sp.inp",
        ),
        _run_inp(job_type="other", selected_inp="/runs/plain/gen/SP.INP"),
        _run_inp(job_type="other", selected_inp="/runs/MeOPh_IRC_TS/gen/calc.inp"),
        _run_inp(job_type="unknown", selected_inp=_SP_IN_IRC_JOB),
        _run_inp(selected_inp=_SP_IN_IRC_JOB),
        _run_inp(selected_inp_name="sp.inp", selected_inp=f"{_IRC_NAMED_JOB}/x/irc.inp"),
        # Former positive filename hints are no operation evidence either.
        _run_inp(job_type="other", selected_inp="/runs/a/gen/irc.inp"),
        _run_inp(job_type="other", selected_inp="/runs/a/gen/IRC_forward.inp"),
        _run_inp(job_type="other", selected_inp="/runs/a/gen/neb-ci.inp"),
        _run_inp(job_type="other", selected_inp="/runs/a/gen/optts.inp"),
        _run_inp(job_type="other", selected_inp="/runs/a/gen/ts_tsopt.inp"),
        _run_inp(job_type="other", selected_inp="/runs/a/gen/opt.inp"),
        _run_inp(job_type="other", selected_inp="/runs/a/gen/freq.inp"),
        _run_inp(job_type="other", selected_inp="/runs/a/gen/numfreq.inp"),
        _run_inp(job_type="other", selected_inp="/runs/a/gen/md.inp"),
        _run_inp(job_type="other", selected_inp=r"C:\runs\TS_IRC\gen\irc.inp"),
        # Words that only contain a hint, and names whose words disagree.
        _run_inp(job_type="other", selected_inp="/runs/a/gen/tsp.inp"),
        _run_inp(job_type="other", selected_inp="/runs/a/gen/spectrum.inp"),
        _run_inp(job_type="other", selected_inp="/runs/a/gen/optimal.inp"),
        _run_inp(job_type="other", selected_inp="/runs/a/gen/freqs.inp"),
        _run_inp(job_type="other", selected_inp="/runs/a/gen/circle.inp"),
        _run_inp(job_type="other", selected_inp="/runs/a/gen/irc_sp.inp"),
        _run_inp(job_type="other", selected_inp="/runs/a/gen/MeOPh_OH_TSD_IRC_F_sp.inp"),
        # Unknown, empty and absent metadata.
        {},
        _run_inp(),
        _run_inp(job_type="", selected_inp=""),
        _run_inp(job_type="unknown"),
        _run_inp(job_type="other", selected_inp="/runs/MeOPh_IRC/gen/"),
    ],
    ids=[
        "legacy-sp-under-irc-ts-parent",
        "legacy-windows-path",
        "legacy-uppercase-basename",
        "legacy-arbitrary-name-under-irc-parent",
        "unknown-type-sp-basename",
        "absent-type-sp-basename",
        "selected-inp-name",
        "irc-basename",
        "irc-token-basename",
        "neb-ci-basename",
        "optts-basename",
        "ts-tsopt-basename",
        "opt-basename",
        "freq-basename",
        "numfreq-basename",
        "md-basename",
        "windows-irc-basename-under-ts-irc-parent",
        "tsp",
        "spectrum",
        "optimal",
        "freqs",
        "circle",
        "irc-and-sp-disagree",
        "parent-words-in-basename-disagree",
        "empty-metadata",
        "generic-task-only",
        "empty-type-and-input",
        "unknown-type-without-input",
        "directory-without-basename",
    ],
)
def test_orca_detail_never_reads_an_operation_from_input_or_directory_names(
    metadata: dict[str, Any],
) -> None:
    assert activity_labels.infer_orca_detail_from_metadata(metadata) == "Unknown"


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"task_kind": "orca", "detail_kind": "ts+freq"}, "TS+Freq"),
        ({"task_kind": "orca"}, "Unknown"),
        ({"task_kind": "nonsense"}, "Unknown"),
        ({"job_type": "nonsense"}, "Unknown"),
        ({"job_type": "orca"}, "Unknown"),
        (_run_inp(task_kind="orca", detail_kind="ts+irc"), "TS+IRC"),
    ],
    ids=[
        "orca-task-kind-defers-to-detail",
        "generic-orca-task",
        "nonsense-task",
        "nonsense-job-type",
        "orca-job-type",
        "orca-run-inp-detail-irc",
    ],
)
def test_orca_detail_never_shows_engine_identity_or_arbitrary_type_strings(
    metadata: dict[str, Any], expected: str
) -> None:
    assert activity_labels.infer_orca_detail_from_metadata(metadata) == expected


def test_queue_detail_text_never_shows_engine_identity_for_generic_orca_task_kind() -> None:
    assert (
        activity_labels.queue_detail_text({"engine": "orca", "metadata": {"task_kind": "orca"}})
        == "Unknown"
    )


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        # Route evidence recorded at admission decides; the file name never does.
        (_run_inp(job_type="other", detail_kind="sp", selected_inp="/r/IRC/g/irc.inp"), "SP"),
        (_run_inp(job_type="other", detail_kind="irc", selected_inp=_SP_IN_IRC_JOB), "IRC"),
        (_run_inp(job_type="other", detail_kind="neb", selected_inp="/r/a/g/calc.inp"), "NEB"),
        # Legacy "other" carries no certainty about the operation.
        (_run_inp(job_type="other", detail_kind="other", selected_inp=_SP_IN_IRC_JOB), "Unknown"),
        (_run_inp(job_type="other", detail_kind="unsupported"), "Other"),
        (_run_inp(job_type="ts", detail_kind="unsupported"), "Other"),
        (_run_inp(job_type="other", detail_kind="neb-idpp"), "NEB-IDPP"),
        (_run_inp(job_type="other", detail_kind="neb-mmfts"), "NEB-MMFTS"),
        (_run_inp(job_type="other", detail_kind="Unsupported"), "Unknown"),
        ({"job_type": "unsupported"}, "Unknown"),
        ({"task_kind": "unsupported"}, "Unknown"),
        ({"task_kind": "optts", "detail_kind": "unsupported"}, "OptTS"),
        # An unrecognized recorded value is no evidence.
        (_run_inp(job_type="other", detail_kind="bogus", selected_inp=_SP_IN_IRC_JOB), "Unknown"),
        (_run_inp(job_type="other", detail_kind="Opt", selected_inp="/r/a/g/opt.inp"), "Unknown"),
        (_run_inp(job_type="other", detail_kind="SP", selected_inp=_SP_IN_IRC_JOB), "Unknown"),
        # Recorded detail_kind outranks coarse job_type when recognized.
        (_run_inp(job_type="opt", detail_kind="sp", selected_inp=_SP_IN_IRC_JOB), "SP"),
        (_run_inp(job_type="ts", detail_kind="ts+freq", selected_inp=_SP_IN_IRC_JOB), "TS+Freq"),
        (_run_inp(job_type="freq", detail_kind="freq", selected_inp="/r/a/g/irc.inp"), "Freq"),
        (_run_inp(job_type="sp", detail_kind="sp", selected_inp="/r/a/g/irc.inp"), "SP"),
        ({"task_kind": "irc", "job_type": "opt", "selected_inp": _SP_IN_IRC_JOB}, "IRC"),
        ({"task_kind": "optts", "job_type": "other", "detail_kind": "sp"}, "OptTS"),
        (_run_inp(job_type="other", detail_kind="ts+irc", selected_inp=_SP_IN_IRC_JOB), "TS+IRC"),
        (_run_inp(job_type="ts", detail_kind="unknown", selected_inp=_SP_IN_IRC_JOB), "Unknown"),
        (
            _run_inp(job_type="other", detail_kind="neb-ts", selected_inp="/r/a/g/calc.inp"),
            "NEB-TS",
        ),
    ],
    ids=[
        "detail-sp-over-irc-basename",
        "detail-irc-over-sp-basename",
        "detail-neb",
        "detail-other",
        "definite-unsupported",
        "unsupported-over-coarse-ts",
        "detail-neb-idpp",
        "detail-neb-mmfts",
        "unsupported-label-is-not-a-token",
        "unsupported-coarse-type-is-no-evidence",
        "unsupported-task-type-is-no-evidence",
        "specific-task-over-unsupported-detail",
        "unknown-detail-is-no-evidence",
        "label-cased-detail-is-not-a-kind",
        "upper-case-detail-is-not-a-kind",
        "detail-over-job-type",
        "detail-ts-freq-over-ts",
        "detail-freq-over-freq-path",
        "detail-sp-over-sp-path",
        "task-kind-over-job-type",
        "task-kind-over-detail",
        "detail-ts-irc",
        "detail-unknown-over-ts",
        "detail-neb-ts",
    ],
)
def test_orca_detail_prefers_explicit_type_then_route_evidence(
    metadata: dict[str, Any], expected: str
) -> None:
    assert activity_labels.infer_orca_detail_from_metadata(metadata) == expected


_PUBLICATION_PENDING = {
    "publication_blocked_reason": "terminal publication pending",
    "publication_blocked_scope": "orca_terminal_publication",
}


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            _run_inp(job_type="other", detail_kind="sp", **_PUBLICATION_PENDING),
            "SP (result publication pending)",
        ),
        (
            _run_inp(job_type="other", selected_inp=_SP_IN_IRC_JOB, **_PUBLICATION_PENDING),
            "Unknown (result publication pending)",
        ),
        (
            _run_inp(
                job_type="other",
                detail_kind="sp",
                publication_blocked_reason="index unavailable",
                publication_blocked_scope="orca_queue",
            ),
            "SP (waiting for publication repair)",
        ),
        (
            _run_inp(
                job_type="other", detail_kind="sp", admission_deferral_reason="scratch is full"
            ),
            "SP (waiting for resources)",
        ),
        (
            _run_inp(
                job_type="other",
                selected_inp=_SP_IN_IRC_JOB,
                admission_deferral_reason="scratch is full",
            ),
            "Unknown (waiting for resources)",
        ),
        (
            _run_inp(job_type="other", detail_kind="unsupported", **_PUBLICATION_PENDING),
            "Other (result publication pending)",
        ),
        (
            _run_inp(
                job_type="other",
                detail_kind="unsupported",
                admission_deferral_reason="scratch is full",
            ),
            "Other (waiting for resources)",
        ),
        (
            _run_inp(job_type="other", detail_kind="neb-idpp", **_PUBLICATION_PENDING),
            "NEB-IDPP (result publication pending)",
        ),
    ],
    ids=[
        "sp-publication-pending",
        "legacy-publication-pending",
        "sp-publication-repair",
        "sp-resources",
        "legacy-resources",
        "unsupported-publication-pending",
        "unsupported-resources",
        "neb-idpp-publication-pending",
    ],
)
def test_orca_detail_text_keeps_the_publication_and_resource_suffixes(
    metadata: dict[str, Any], expected: str
) -> None:
    assert activity_labels.queue_detail_text({"engine": "orca", "metadata": metadata}) == expected


def test_queue_list_table_shows_sp_only_for_recorded_single_points_under_an_irc_named_job(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        activity_labels,
        "queue_table_now",
        lambda: datetime(2026, 10, 1, 0, 10, 0, tzinfo=UTC),
    )
    rows = [
        {
            "activity_id": queue_id,
            "kind": "job",
            "engine": "orca",
            "status": "pending",
            "label": "MeOPh_OH_TSD_IRC_F_sp_continuous_01",
            "submitted_at": "2026-10-01T00:00:00+00:00",
            "metadata": _run_inp(
                job_type=job_type,
                selected_inp=f"{_IRC_NAMED_JOB}/20261001-000000-0123abcd/{name}",
                reaction_dir=_IRC_NAMED_JOB,
                **({"detail_kind": detail_kind} if detail_kind else {}),
            ),
        }
        for queue_id, name, job_type, detail_kind in (
            ("q-new-sp", "sp.inp", "other", "sp"),
            ("q-new-freq-block", "sp.inp", "other", "unknown"),
            ("q-legacy-sp", "sp.inp", "other", None),
            ("q-opt", "opt.inp", "opt", None),
            ("q-legacy-irc", "irc.inp", "other", None),
            ("q-new-irc", "sp.inp", "other", "irc"),
            ("q-new-unsupported", "sp.inp", "other", "unsupported"),
            ("q-legacy-other", "sp.inp", "other", "other"),
            ("q-new-idpp", "sp.inp", "other", "neb-idpp"),
        )
    ]

    lines = _table_lines(rows)

    # Status, Name, Detail, ID, Elapsed; no cell holds a space here.
    details = {line.split()[3]: line.split()[2] for line in lines[2:]}
    assert details == {
        "q-new-sp": "SP",
        "q-new-freq-block": "Unknown",
        "q-legacy-sp": "Unknown",
        "q-opt": "Opt",
        "q-legacy-irc": "Unknown",
        "q-new-irc": "IRC",
        "q-new-unsupported": "Other",
        "q-legacy-other": "Unknown",
        "q-new-idpp": "NEB-IDPP",
    }


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
