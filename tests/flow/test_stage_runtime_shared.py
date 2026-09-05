"""Submission-failure metadata on the live per-stage path.

Both tests are retargeted regressions from the removed workflow-level
submitter cluster. The incident: three OptTS submissions rejected by the
execution-snapshot basename gate with no reason recorded anywhere — the fix
originally landed in the cluster's `record_submission_outcome`, which
production never reached. The live path recorder now carries the behavior.
"""

from __future__ import annotations

from pathlib import Path
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


def test_rejected_submission_detail_truncates_and_omits_when_blank() -> None:
    stage_metadata: dict[str, Any] = {}
    _apply(
        stage_metadata,
        {
            "status": "failed",
            "reason": "invalid_submission_input",
            "returncode": 1,
            "stderr": "x" * 5000,
            "stdout": "",
            "submitted_at": "2026-08-02T00:00:00+00:00",
        },
    )
    assert len(stage_metadata["submission_error_detail"]) == 1000

    blank_metadata: dict[str, Any] = {
        "submission_error_detail": "detail from the previous failed attempt"
    }
    _apply(
        blank_metadata,
        {
            "status": "failed",
            "reason": "invalid_submission_input",
            "returncode": 1,
            "stderr": "  ",
            "stdout": "",
            "submitted_at": "2026-08-02T00:00:00+00:00",
        },
    )
    assert "submission_error_detail" not in blank_metadata
    assert blank_metadata["reason"] == "invalid_submission_input"


def test_deferred_submission_leaves_failure_metadata_untouched() -> None:
    stage_metadata: dict[str, Any] = {
        "reason": "invalid_submission_input",
        "submission_error_detail": "old failure detail",
    }
    stage, _task = _apply(
        stage_metadata,
        {
            "status": "waiting_for_slot",
            "reason": "submission_conflict",
            "returncode": 0,
            "stderr": "",
            "stdout": "",
            "submitted_at": "2026-08-02T00:00:00+00:00",
        },
    )
    # Mirrors the removed recorder: deferral neither records nor clears the
    # failure keys; only a submit or a fail transition owns them.
    assert stage["status"] == "planned"
    assert stage_metadata["reason"] == "invalid_submission_input"
    assert stage_metadata["submission_error_detail"] == "old failure detail"


def test_orca_sync_defers_contract_pass_after_rejection_and_keeps_reason(
    tmp_path: Path,
) -> None:
    """A rejected submission owns its tick before an older contract can apply.

    A later sync may still use the contract to reattach a live job; only the
    tick that persisted this new rejection must skip the stale lookup.
    """
    from unittest.mock import Mock

    from orca_auto.flow.contracts import OrcaArtifactContract
    from orca_auto.flow.orchestration.stage_runtime.orca import sync_orca_stage_impl
    from tests.flow.orchestration_services import orchestration_services

    reaction_dir = tmp_path / "rxn_rejected"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "ts_guess.inp"
    selected_inp.write_text(
        "! HF Opt\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )
    stage: dict[str, Any] = {
        "stage_id": "orca_optts_freq_01",
        "stage_kind": "orca_stage",
        "status": "planned",
        "metadata": {},
        "task": {
            "engine": "orca",
            "task_kind": "opt",
            "status": "planned",
            "payload": {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(selected_inp),
            },
            "enqueue_payload": {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(selected_inp),
                "priority": 10,
            },
        },
    }
    rejection = {
        "status": "failed",
        "reason": "invalid_submission_input",
        "returncode": 1,
        "stderr": (
            "ORCA referenced input basename conflicts with a generation "
            "runtime/output file: ts_guess.hess\n"
        ),
        "stdout": "",
        "parsed_stdout": {},
    }
    stale_terminal_contract = OrcaArtifactContract(
        run_id="run_from_previous_submission",
        status="failed",
        reason="old_run_failed",
        state_status="failed",
        reaction_dir=str(reaction_dir),
        latest_known_path="",
        optimized_xyz_path="",
        queue_id="",
        queue_status="",
        cancel_requested=False,
        selected_inp="",
        selected_input_xyz="",
        analyzer_status="",
        completed_at="",
        last_out_path="",
        run_state_path="",
        report_json_path="",
        attempt_count=0,
        attempts=(),
        final_result={},
    )
    contract_loader = Mock(return_value=stale_terminal_contract)
    deps = orchestration_services(
        overrides={
            "submit_reaction_dir": Mock(return_value=rejection),
            "load_orca_artifact_contract": contract_loader,
        }
    )

    sync_orca_stage_impl(
        stage,
        orca_config="/tmp/orca_auto.yaml",
        orca_repo_root=None,
        submit_ready=True,
        services=deps,
    )

    metadata = stage["metadata"]
    assert isinstance(metadata, dict)
    assert stage["status"] == "submission_failed"
    assert metadata["reason"] == "invalid_submission_input"
    assert "ts_guess.hess" in metadata["submission_error_detail"]
    assert contract_loader.call_count == 0

    # A terminal contract from an earlier submission can still be present on
    # the next tick. It must not replace the new rejection or mix its reason
    # with the new submission detail.
    sync_orca_stage_impl(
        stage,
        orca_config="/tmp/orca_auto.yaml",
        orca_repo_root=None,
        submit_ready=True,
        services=deps,
    )
    assert stage["status"] == "submission_failed"
    assert stage["task"]["status"] == "submission_failed"
    assert metadata["reason"] == "invalid_submission_input"
    assert "ts_guess.hess" in metadata["submission_error_detail"]
    assert contract_loader.call_count == 1


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
