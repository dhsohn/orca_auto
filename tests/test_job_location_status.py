from __future__ import annotations

import pytest

from orca_auto.orca.job_locations import _utils as _canonical_orca_status


@pytest.mark.parametrize(
    ("queue_entry", "state", "report", "expected"),
    [
        (
            {"task_id": "job_1", "status": "running"},
            {"job_id": "job_1", "run_id": "run_1", "status": "running"},
            {
                "job_id": "job_1",
                "run_id": "run_1",
                "final_result": {
                    "status": "completed",
                    "analyzer_status": "completed",
                    "reason": "normal_termination",
                    "completed_at": "2026-04-19T00:10:00+00:00",
                },
            },
            ("completed", "completed", "normal_termination", "2026-04-19T00:10:00+00:00"),
        ),
        (
            {"status": "cancelled"},
            {},
            {},
            ("cancelled", "", "cancelled", ""),
        ),
        (
            {"status": "running", "cancel_requested": True},
            {},
            {},
            ("cancel_requested", "", "", ""),
        ),
        (
            {"status": "pending"},
            {},
            {},
            ("queued", "", "", ""),
        ),
        (
            {"task_id": "job_1"},
            {"job_id": "job_1", "run_id": "run_1", "status": "retrying"},
            {},
            ("running", "", "", ""),
        ),
        (
            {"task_id": "job_1"},
            {},
            {"job_id": "job_1", "run_id": "run_1", "status": "failed"},
            ("failed", "", "", ""),
        ),
        (
            None,
            {},
            {},
            ("unknown", "", "", ""),
        ),
    ],
)
def test_status_from_payloads_covers_priority_order(
    queue_entry: dict[str, object] | None,
    state: dict[str, object],
    report: dict[str, object],
    expected: tuple[str, str, str, str],
) -> None:
    assert (
        _canonical_orca_status.status_from_payloads(
            queue_entry=queue_entry,
            state=state,
            report=report,
        )
        == expected
    )


def test_status_ignores_terminal_payload_from_previous_queue_generation() -> None:
    old_final = {
        "status": "failed",
        "analyzer_status": "incomplete",
        "reason": "old_failure",
        "completed_at": "2026-04-19T00:10:00+00:00",
    }

    status = _canonical_orca_status.status_from_payloads(
        queue_entry={"queue_id": "q_new", "task_id": "job_new", "status": "pending"},
        state={"job_id": "job_old", "run_id": "run_old", "status": "failed"},
        report={
            "job_id": "job_old",
            "run_id": "run_old",
            "status": "failed",
            "final_result": old_final,
        },
    )

    assert status == ("queued", "", "", "")


def test_status_accepts_matching_terminal_payload_before_queue_finalization() -> None:
    final = {
        "status": "completed",
        "analyzer_status": "completed",
        "reason": "normal_termination",
        "completed_at": "2026-04-19T00:10:00+00:00",
    }

    status = _canonical_orca_status.status_from_payloads(
        queue_entry={"queue_id": "q_new", "task_id": "job_new", "status": "running"},
        state={
            "job_id": "job_new",
            "run_id": "run_new",
            "status": "completed",
            "final_result": final,
        },
        report={},
    )

    assert status == (
        "completed",
        "completed",
        "normal_termination",
        "2026-04-19T00:10:00+00:00",
    )


def test_attempt_helpers_prefer_report_values_and_coerce_attempt_rows() -> None:
    state = {
        "attempts": [
            {
                "index": 2,
                "inp_path": "/tmp/rxn.retry01.inp",
                "out_path": "/tmp/rxn.retry01.out",
                "return_code": 0,
                "analyzer_status": "completed",
                "analyzer_reason": "normal_termination",
                "markers": ["marker"],
                "patch_actions": ["patch"],
                "started_at": "2026-04-19T00:00:00+00:00",
                "ended_at": "2026-04-19T00:01:00+00:00",
            },
            "skip",
        ],
    }
    report = {
        "attempt_count": "3",
    }

    attempts = _canonical_orca_status.coerce_attempts(state, report)

    assert _canonical_orca_status.attempt_count(state, report) == 3
    assert attempts == (
        {
            "index": 2,
            "attempt_number": 1,
            "inp_path": "/tmp/rxn.retry01.inp",
            "out_path": "/tmp/rxn.retry01.out",
            "return_code": 0,
            "analyzer_status": "completed",
            "analyzer_reason": "normal_termination",
            "markers": ["marker"],
            "patch_actions": ["patch"],
            "started_at": "2026-04-19T00:00:00+00:00",
            "ended_at": "2026-04-19T00:01:00+00:00",
        },
    )


def test_attempt_helpers_preserve_mapping_markers() -> None:
    markers = {
        "terminated_normally": True,
        "imaginary_frequency_count": 1,
    }

    attempts = _canonical_orca_status.coerce_attempts(
        {"attempts": [{"index": 1, "markers": markers}]},
        {},
    )

    assert attempts[0]["markers"] == markers
