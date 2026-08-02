"""Submission-failure metadata on the live per-stage path.

Both tests are retargeted regressions from the removed workflow-level
submitter cluster. The incident: three OptTS submissions rejected by the
execution-snapshot basename gate with no reason recorded anywhere — the fix
originally landed in the cluster's `record_submission_outcome`, which
production never reached. The live path recorder now carries the behavior.
"""

from __future__ import annotations

from typing import Any

from orca_auto.flow.orchestration.stage_runtime.shared import _apply_submission_result


def _apply(
    stage_metadata: dict[str, Any],
    submission: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    stage: dict[str, Any] = {"stage_id": "orca_optts_freq_01"}
    task: dict[str, Any] = {}
    _apply_submission_result(
        stage=stage,
        task=task,
        stage_metadata=stage_metadata,
        submission=submission,
        metadata_fields=(
            ("queue_id", "queue_id"),
            ("submission_status", "status"),
            ("submitted_at", "submitted_at"),
        ),
    )
    return stage, task


def test_rejected_submission_persists_reason_and_detail() -> None:
    stage_metadata: dict[str, Any] = {}
    stage, task = _apply(
        stage_metadata,
        {
            "status": "failed",
            "reason": "invalid_submission_input",
            "returncode": 1,
            "stderr": (
                "ORCA referenced input basename conflicts with a generation "
                "runtime/output file: ts_guess.hess\n"
            ),
            "stdout": "",
            "parsed_stdout": {},
            "submitted_at": "2026-08-02T00:00:00+00:00",
        },
    )

    assert stage["status"] == "submission_failed"
    assert stage_metadata["reason"] == "invalid_submission_input"
    assert "ts_guess.hess" in stage_metadata["submission_error_detail"]


def test_rejected_submission_without_reason_gets_the_generic_reason() -> None:
    stage_metadata: dict[str, Any] = {}
    _apply(
        stage_metadata,
        {
            "status": "failed",
            "returncode": 1,
            "stderr": "",
            "stdout": "engine refused the submission",
            "submitted_at": "2026-08-02T00:00:00+00:00",
        },
    )

    assert stage_metadata["reason"] == "queue_submission_failed"
    assert stage_metadata["submission_error_detail"] == "engine refused the submission"


def test_successful_resubmission_clears_stale_failure_detail() -> None:
    stage_metadata: dict[str, Any] = {
        "reason": "invalid_submission_input",
        "submission_error_detail": "old failure detail",
        "submission_deferred_reason": "submission_conflict",
    }
    stage, task = _apply(
        stage_metadata,
        {
            "status": "submitted",
            "returncode": 0,
            "stdout": "status: queued\nqueue_id: q_new\n",
            "stderr": "",
            "parsed_stdout": {"status": "queued", "queue_id": "q_new"},
            "queue_id": "q_new",
            "submitted_at": "2026-08-02T00:00:00+00:00",
        },
    )

    assert stage["status"] == "queued"
    assert "reason" not in stage_metadata
    assert "submission_error_detail" not in stage_metadata
    assert "submission_deferred_reason" not in stage_metadata
