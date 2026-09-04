from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orca_auto.flow.orchestration.stage_runtime import xtb_sync as xtb_sync_runtime
from orca_auto.flow.orchestration.stage_runtime.xtb_sync import sync_xtb_stage_impl
from orca_auto.flow.orchestration.stage_views import WorkflowStageView
from tests.flow.orchestration_services import orchestration_services


def test_sync_xtb_stage_submits_initial_attempt_and_records_handoff_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = SimpleNamespace(
        status="completed",
        job_id="xtb_job_01",
        reason="ok",
        latest_known_path="/tmp/xtb_done",
        selected_input_xyz="/tmp/xtb_done/reactant.xyz",
        candidate_details=(
            SimpleNamespace(
                path="/tmp/xtb_done/ts_guess.xyz",
                selected=True,
                rank=1,
                kind="ts_guess",
                score=-12.3,
                metadata={"source": "xtb"},
            ),
        ),
        selected_candidate_paths=["/tmp/xtb_done/ts_guess.xyz"],
        analysis_summary={"completed_at": "2026-04-19T00:10:00+00:00"},
    )
    stage: dict[str, Any] = {
        "stage_id": "xtb_path_search_01",
        "status": "planned",
        "metadata": {},
        "task": {
            "engine": "xtb",
            "task_kind": "path_search",
            "status": "planned",
            "payload": {"job_dir": "", "selected_input_xyz": ""},
            "enqueue_payload": {"priority": 7},
        },
    }

    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda config_path, **kwargs: {
                "allowed_root": tmp_path / "xtb_allowed"
            },
            "submit_xtb_job_dir": lambda **kwargs: {
                "status": "submitted",
                "queue_id": "q_xtb_01",
                "job_id": "xtb_job_01",
            },
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "now_utc_iso": lambda: "2026-04-19T14:00:00+00:00",
        }
    )
    monkeypatch.setattr(
        xtb_sync_runtime,
        "ensure_xtb_job_dir_impl",
        lambda stage, **kwargs: str(tmp_path / "xtb_allowed" / "wf_01" / "job_01"),
    )
    monkeypatch.setattr(
        xtb_sync_runtime,
        "xtb_handoff_status_impl",
        lambda current_contract, **kwargs: {
            "status": "ready",
            "reason": "",
            "message": "",
            "artifact_path": "/tmp/xtb_done/ts_guess.xyz",
        },
    )

    sync_xtb_stage_impl(
        stage,
        xtb_config="/tmp/xtb.yaml",
        submit_ready=True,
        workflow_id="wf_01",
        workspace_dir=tmp_path / "workspace" / "wf_01",
        services=deps,
    )

    metadata = stage["metadata"]
    task = stage["task"]
    attempt = metadata["xtb_attempts"][0]

    assert stage["status"] == "completed"
    assert task["status"] == "completed"
    assert task["submission_result"]["queue_id"] == "q_xtb_01"
    assert task["submission_result"]["submitted_at"] == "2026-04-19T14:00:00+00:00"
    assert task["payload"]["selected_input_xyz"] == "/tmp/xtb_done/reactant.xyz"
    assert metadata["queue_id"] == "q_xtb_01"
    assert metadata["child_job_id"] == "xtb_job_01"
    assert metadata["reaction_handoff_status"] == "ready"
    assert metadata["reaction_handoff_artifact_path"] == "/tmp/xtb_done/ts_guess.xyz"
    assert metadata["xtb_handoff_retry_limit"] == 2
    assert metadata["xtb_handoff_retries_used"] == 0
    assert attempt["submission_status"] == "submitted"
    assert attempt["queue_id"] == "q_xtb_01"
    assert attempt["status"] == "completed"
    assert attempt["handoff_status"] == "ready"
    assert stage["output_artifacts"] == [
        {
            "kind": "xtb_candidate",
            "path": "/tmp/xtb_done/ts_guess.xyz",
            "selected": True,
            "metadata": {"rank": 1, "kind": "ts_guess", "score": -12.3, "source": "xtb"},
        }
    ]


def test_sync_xtb_stage_retries_after_cancel_deferred_without_applying_old_contract(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "xtb_allowed" / "job"
    submissions = iter(
        (
            {
                "status": "blocked",
                "reason": "cancel_requested",
                "queue_id": "q_old",
                "job_id": "xtb_old",
            },
            {
                "status": "submitted",
                "queue_id": "q_new",
                "job_id": "xtb_new",
            },
        )
    )
    contract_calls = 0

    def load_contract(**_kwargs: Any) -> Any:
        nonlocal contract_calls
        contract_calls += 1
        return SimpleNamespace(
            status="queued",
            job_id="xtb_new",
            reason="",
            latest_known_path=str(job_dir),
            selected_input_xyz=str(job_dir / "input.xyz"),
            candidate_details=(),
            selected_candidate_paths=(),
            analysis_summary={},
        )

    stage: dict[str, Any] = {
        "stage_id": "xtb_restart",
        "status": "planned",
        "metadata": {"queue_id": "q_old"},
        "task": {
            "engine": "xtb",
            "task_kind": "opt",
            "status": "planned",
            "payload": {
                "job_dir": str(job_dir),
                "selected_input_xyz": str(job_dir / "input.xyz"),
            },
            "enqueue_payload": {"priority": 8},
        },
    }
    deps = orchestration_services(
        overrides={
            "submit_xtb_job_dir": lambda **_kwargs: next(submissions),
            "load_xtb_artifact_contract": load_contract,
            "now_utc_iso": lambda: "2026-07-10T00:00:00+00:00",
        }
    )

    sync_xtb_stage_impl(
        stage,
        xtb_config="/tmp/xtb.yaml",
        submit_ready=True,
        workflow_id="wf_restart",
        workspace_dir=tmp_path / "workspace",
        services=deps,
    )

    assert stage["status"] == "planned"
    assert stage["task"]["status"] == "planned"
    assert stage["metadata"]["submission_deferred_reason"] == "cancel_requested"
    assert stage["metadata"]["xtb_handoff_status"] == "waiting_for_slot"
    assert "reaction_handoff_status" not in stage["metadata"]
    assert "xtb_handoff_retries_used" not in stage["metadata"]
    assert contract_calls == 0

    sync_xtb_stage_impl(
        stage,
        xtb_config="/tmp/xtb.yaml",
        submit_ready=True,
        workflow_id="wf_restart",
        workspace_dir=tmp_path / "workspace",
        services=deps,
    )

    assert contract_calls == 1
    assert stage["status"] == "queued"
    assert stage["task"]["status"] == "queued"
    assert stage["metadata"]["queue_id"] == "q_new"
    assert stage["metadata"]["child_job_id"] == "xtb_new"
    assert stage["metadata"]["xtb_handoff_status"] == "submitted"
    assert "reaction_handoff_status" not in stage["metadata"]
    assert stage["metadata"]["xtb_handoff_retries_used"] == 0
    assert "submission_deferred_reason" not in stage["metadata"]


def test_sync_xtb_stage_does_not_apply_old_contract_after_submission_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "xtb_allowed" / "job"
    stale_terminal_contract = SimpleNamespace(
        status="failed",
        job_id="xtb_old",
        reason="old_xtb_failure",
        latest_known_path="/tmp/xtb_old",
        selected_input_xyz="/tmp/xtb_old/input.xyz",
        candidate_details=(),
        selected_candidate_paths=(),
        analysis_summary={},
    )
    contract_calls = 0

    def load_contract(**_kwargs: Any) -> Any:
        nonlocal contract_calls
        contract_calls += 1
        return stale_terminal_contract

    stage: dict[str, Any] = {
        "stage_id": "xtb_rejected",
        "status": "planned",
        "metadata": {
            "queue_id": "q_old",
            "child_job_id": "xtb_old",
            "xtb_handoff_status": "submitted",
        },
        "task": {
            "engine": "xtb",
            "task_kind": "path_search",
            "status": "planned",
            "payload": {
                "job_dir": str(job_dir),
                "selected_input_xyz": str(job_dir / "input.xyz"),
            },
            "enqueue_payload": {"priority": 8},
        },
    }
    deps = orchestration_services(
        overrides={
            "submit_xtb_job_dir": lambda **_kwargs: {
                "status": "failed",
                "reason": "invalid_submission_input",
                "stderr": "rejected new xTB submission",
            },
            "load_xtb_artifact_contract": load_contract,
            "now_utc_iso": lambda: "2026-09-04T00:00:00+00:00",
        }
    )
    monkeypatch.setattr(
        xtb_sync_runtime,
        "xtb_handoff_status_impl",
        lambda _contract, **_kwargs: {
            "status": "failed",
            "reason": "old_handoff_failure",
            "message": "old handoff failed",
            "artifact_path": "",
        },
    )

    sync_xtb_stage_impl(
        stage,
        xtb_config="/tmp/xtb.yaml",
        submit_ready=True,
        workflow_id="wf_rejected",
        workspace_dir=tmp_path / "workspace",
        services=deps,
    )

    assert stage["status"] == "submission_failed"
    assert stage["task"]["status"] == "submission_failed"
    assert stage["metadata"]["reason"] == "invalid_submission_input"
    assert "child_job_id" not in stage["metadata"]
    assert "xtb_handoff_status" not in stage["metadata"]
    assert contract_calls == 0

    sync_xtb_stage_impl(
        stage,
        xtb_config="/tmp/xtb.yaml",
        submit_ready=False,
        workflow_id="wf_rejected",
        workspace_dir=tmp_path / "workspace",
        services=deps,
    )

    assert contract_calls == 1
    assert stage["status"] == "submission_failed"
    assert stage["task"]["status"] == "submission_failed"
    assert stage["metadata"]["reason"] == "invalid_submission_input"
    assert "child_job_id" not in stage["metadata"]


def test_sync_xtb_stage_retries_failed_handoff_when_retry_budget_remains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = SimpleNamespace(
        status="completed",
        job_id="xtb_job_02",
        reason="ts_missing",
        latest_known_path="/tmp/xtb_done",
        selected_input_xyz="/tmp/xtb_done/reactant.xyz",
        candidate_details=(),
        selected_candidate_paths=[],
        analysis_summary={"completed_at": "2026-04-19T00:20:00+00:00"},
    )
    stage: dict[str, Any] = {
        "stage_id": "xtb_path_search_02",
        "status": "completed",
        "metadata": {"xtb_handoff_retries_used": 0},
        "task": {
            "engine": "xtb",
            "task_kind": "path_search",
            "status": "completed",
            "payload": {"job_dir": "/tmp/original_job", "max_handoff_retries": 2},
            "enqueue_payload": {"priority": 9},
        },
    }
    submissions: list[dict[str, Any]] = []

    def fake_submit_xtb_job_dir(**kwargs: Any) -> dict[str, str]:
        submissions.append(kwargs)
        return {"status": "submitted", "queue_id": "q_retry_01", "job_id": "xtb_job_retry"}

    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda config_path, **kwargs: {
                "allowed_root": tmp_path / "xtb_allowed"
            },
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "submit_xtb_job_dir": fake_submit_xtb_job_dir,
            "now_utc_iso": lambda: "2026-04-19T14:10:00+00:00",
        }
    )
    monkeypatch.setattr(
        xtb_sync_runtime,
        "xtb_handoff_status_impl",
        lambda current_contract, **kwargs: {
            "status": "failed",
            "reason": "xtb_ts_guess_missing",
            "message": "missing ts guess",
            "artifact_path": "",
        },
    )
    monkeypatch.setattr(
        xtb_sync_runtime,
        "write_xtb_path_job_impl",
        lambda stage, **kwargs: str(tmp_path / "xtb_allowed" / "wf_02" / "retry_attempt_01"),
    )

    sync_xtb_stage_impl(
        stage,
        xtb_config="/tmp/xtb.yaml",
        submit_ready=True,
        workflow_id="wf_02",
        workspace_dir=tmp_path / "workspace" / "wf_02",
        services=deps,
    )

    metadata = stage["metadata"]
    retry_attempt = next(
        item
        for item in cast(list[dict[str, Any]], metadata["xtb_attempts"])
        if item["attempt_number"] == 1
    )

    assert submissions and submissions[0]["job_dir"].endswith("retry_attempt_01")
    assert stage["status"] == "queued"
    assert stage["task"]["status"] == "submitted"
    assert stage["task"]["payload"]["job_dir"].endswith("retry_attempt_01")
    assert stage["task"]["submission_result"]["queue_id"] == "q_retry_01"
    assert metadata["child_job_id"] == "xtb_job_retry"
    assert metadata["queue_id"] == "q_retry_01"
    assert metadata["latest_known_path"].endswith("retry_attempt_01")
    assert metadata["xtb_handoff_status"] == "retrying"
    assert metadata["reaction_handoff_status"] == "retrying"
    assert metadata["xtb_handoff_retries_used"] == 1
    assert metadata["xtb_handoff_retry_limit"] == 2
    assert retry_attempt["submission_status"] == "submitted"
    assert retry_attempt["trigger_reason"] == "xtb_ts_guess_missing"
    assert retry_attempt["trigger_message"] == "missing ts guess"


def test_sync_xtb_stage_does_not_consume_retry_when_handoff_submission_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = SimpleNamespace(
        status="completed",
        job_id="xtb_job_old",
        reason="ts_missing",
        latest_known_path="/tmp/xtb_done",
        selected_input_xyz="/tmp/xtb_done/reactant.xyz",
        candidate_details=(),
        selected_candidate_paths=[],
        analysis_summary={"completed_at": "2026-04-19T00:20:00+00:00"},
    )
    stage: dict[str, Any] = {
        "stage_id": "xtb_path_search_rejected_retry",
        "status": "completed",
        "metadata": {"xtb_handoff_retries_used": 0},
        "task": {
            "engine": "xtb",
            "task_kind": "path_search",
            "status": "completed",
            "payload": {"job_dir": "/tmp/original_job", "max_handoff_retries": 2},
            "enqueue_payload": {"priority": 9},
        },
    }
    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda config_path, **kwargs: {
                "allowed_root": tmp_path / "xtb_allowed"
            },
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "submit_xtb_job_dir": lambda **kwargs: {
                "status": "failed",
                "reason": "invalid_submission_input",
                "stderr": "retry input was rejected",
            },
            "now_utc_iso": lambda: "2026-09-04T00:10:00+00:00",
        }
    )
    monkeypatch.setattr(
        xtb_sync_runtime,
        "xtb_handoff_status_impl",
        lambda current_contract, **kwargs: {
            "status": "failed",
            "reason": "xtb_ts_guess_missing",
            "message": "missing ts guess",
            "artifact_path": "",
        },
    )
    monkeypatch.setattr(
        xtb_sync_runtime,
        "write_xtb_path_job_impl",
        lambda stage, **kwargs: str(
            tmp_path / "xtb_allowed" / "wf_rejected_retry" / "retry_attempt_01"
        ),
    )

    sync_xtb_stage_impl(
        stage,
        xtb_config="/tmp/xtb.yaml",
        submit_ready=True,
        workflow_id="wf_rejected_retry",
        workspace_dir=tmp_path / "workspace" / "wf_rejected_retry",
        services=deps,
    )

    metadata = stage["metadata"]
    assert stage["status"] == "submission_failed"
    assert stage["task"]["status"] == "submission_failed"
    assert metadata["reason"] == "invalid_submission_input"
    assert "retry input was rejected" in metadata["submission_error_detail"]
    assert metadata["reaction_handoff_status"] == "failed"
    assert metadata["reaction_handoff_reason"] == "xtb_ts_guess_missing"
    assert metadata["xtb_handoff_retries_used"] == 0
    assert "xtb_handoff_status" not in metadata
    assert "child_job_id" not in metadata


def test_pending_xtb_handoff_retry_requires_the_materialized_retry_job_dir() -> None:
    retry_job_dir = "/tmp/xtb_retry_attempt_01"
    stage: dict[str, Any] = {
        "metadata": {
            "xtb_active_attempt_number": 1,
            "xtb_attempts": [
                {
                    "attempt_number": 1,
                    "job_dir": retry_job_dir,
                    "submission_status": "blocked",
                }
            ],
            "reaction_handoff_status": "retrying",
            "xtb_handoff_status": "waiting_for_slot",
            "xtb_handoff_retries_used": 0,
            "xtb_handoff_retry_limit": 2,
        },
        "task": {
            "engine": "xtb",
            "task_kind": "path_search",
            "status": "planned",
            "payload": {"job_dir": retry_job_dir},
        },
    }
    stage_view = WorkflowStageView(stage)

    decision = xtb_sync_runtime._pending_xtb_handoff_retry_decision(
        stage_view,
        stage_view.task,
    )
    assert decision is not None and decision.next_attempt == 1

    stage["task"]["payload"]["job_dir"] = ""
    assert (
        xtb_sync_runtime._pending_xtb_handoff_retry_decision(
            stage_view,
            stage_view.task,
        )
        is None
    )


@pytest.mark.parametrize("second_status", ["failed", "submitted"])
def test_sync_xtb_stage_preserves_deferred_handoff_retry_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    second_status: str,
) -> None:
    contract = SimpleNamespace(
        status="completed",
        job_id="xtb_job_old",
        reason="ts_missing",
        latest_known_path="/tmp/xtb_done",
        selected_input_xyz="/tmp/xtb_done/reactant.xyz",
        candidate_details=(),
        selected_candidate_paths=[],
        analysis_summary={"completed_at": "2026-04-19T00:20:00+00:00"},
    )
    stage: dict[str, Any] = {
        "stage_id": "xtb_path_search_deferred_retry",
        "status": "completed",
        "metadata": {"xtb_handoff_retries_used": 0},
        "task": {
            "engine": "xtb",
            "task_kind": "path_search",
            "status": "completed",
            "payload": {"job_dir": "/tmp/original_job", "max_handoff_retries": 2},
            "enqueue_payload": {"priority": 9},
        },
    }
    submissions = iter(
        (
            {"status": "blocked", "reason": "cancel_requested"},
            {
                "status": second_status,
                "reason": "invalid_submission_input" if second_status == "failed" else "",
                "stderr": "retry input was rejected" if second_status == "failed" else "",
                "queue_id": "q_retry_01" if second_status == "submitted" else "",
                "job_id": "xtb_job_retry" if second_status == "submitted" else "",
            },
        )
    )
    contract_calls = 0

    def load_contract(**_kwargs: Any) -> Any:
        nonlocal contract_calls
        contract_calls += 1
        if contract_calls == 1:
            return contract
        raise FileNotFoundError("the accepted retry contract is not published yet")

    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda config_path, **kwargs: {
                "allowed_root": tmp_path / "xtb_allowed"
            },
            "load_xtb_artifact_contract": load_contract,
            "submit_xtb_job_dir": lambda **kwargs: next(submissions),
            "now_utc_iso": lambda: "2026-09-04T00:20:00+00:00",
        }
    )
    monkeypatch.setattr(
        xtb_sync_runtime,
        "xtb_handoff_status_impl",
        lambda current_contract, **kwargs: {
            "status": "failed",
            "reason": "xtb_ts_guess_missing",
            "message": "missing ts guess",
            "artifact_path": "",
        },
    )
    retry_job_dir = str(tmp_path / "xtb_allowed" / "wf_deferred_retry" / "retry_attempt_01")

    def write_retry_job(current_stage: dict[str, Any], **_kwargs: Any) -> str:
        current_stage["metadata"]["xtb_active_attempt_number"] = 1
        current_stage["metadata"]["xtb_attempts"].append(
            {"attempt_number": 1, "job_dir": retry_job_dir}
        )
        return retry_job_dir

    monkeypatch.setattr(xtb_sync_runtime, "write_xtb_path_job_impl", write_retry_job)

    sync_xtb_stage_impl(
        stage,
        xtb_config="/tmp/xtb.yaml",
        submit_ready=True,
        workflow_id="wf_deferred_retry",
        workspace_dir=tmp_path / "workspace" / "wf_deferred_retry",
        services=deps,
    )

    metadata = stage["metadata"]
    assert stage["status"] == "planned"
    assert stage["task"]["status"] == "planned"
    assert metadata["reaction_handoff_status"] == "retrying"
    assert metadata["xtb_handoff_status"] == "waiting_for_slot"
    assert metadata["xtb_handoff_retries_used"] == 0
    assert metadata["xtb_attempts"][1]["submission_status"] == "blocked"

    sync_xtb_stage_impl(
        stage,
        xtb_config="/tmp/xtb.yaml",
        submit_ready=True,
        workflow_id="wf_deferred_retry",
        workspace_dir=tmp_path / "workspace" / "wf_deferred_retry",
        services=deps,
    )

    if second_status == "failed":
        assert stage["status"] == "submission_failed"
        assert stage["task"]["status"] == "submission_failed"
        assert metadata["reaction_handoff_status"] == "failed"
        assert metadata["reaction_handoff_reason"] == "xtb_ts_guess_missing"
        assert metadata["xtb_handoff_retries_used"] == 0
        assert "xtb_handoff_status" not in metadata
        assert "child_job_id" not in metadata
        assert contract_calls == 1
    else:
        assert stage["status"] == "queued"
        assert stage["task"]["status"] == "submitted"
        assert metadata["reaction_handoff_status"] == "retrying"
        assert metadata["xtb_handoff_status"] == "retrying"
        assert metadata["xtb_handoff_retries_used"] == 1
        assert metadata["child_job_id"] == "xtb_job_retry"
        assert contract_calls == 2


def test_sync_xtb_stage_stops_retrying_after_limit_and_materializes_empty_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = SimpleNamespace(
        status="failed",
        job_id="xtb_job_03",
        reason="ts_missing",
        latest_known_path="/tmp/xtb_failed",
        selected_input_xyz="/tmp/xtb_failed/reactant.xyz",
        candidate_details=(),
        selected_candidate_paths=[],
        analysis_summary={"completed_at": "2026-04-19T00:30:00+00:00"},
    )
    stage: dict[str, Any] = {
        "stage_id": "xtb_path_search_03",
        "status": "failed",
        "metadata": {"xtb_handoff_retries_used": 2},
        "task": {
            "engine": "xtb",
            "task_kind": "path_search",
            "status": "failed",
            "payload": {"job_dir": "/tmp/original_job", "max_handoff_retries": 2},
            "enqueue_payload": {"priority": 9},
        },
    }

    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda config_path, **kwargs: {
                "allowed_root": tmp_path / "xtb_allowed"
            },
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "submit_xtb_job_dir": lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("should not resubmit once retry limit is exhausted")
            ),
        }
    )
    monkeypatch.setattr(
        xtb_sync_runtime,
        "xtb_handoff_status_impl",
        lambda current_contract, **kwargs: {
            "status": "failed",
            "reason": "xtb_ts_guess_missing",
            "message": "missing ts guess",
            "artifact_path": "",
        },
    )

    sync_xtb_stage_impl(
        stage,
        xtb_config="/tmp/xtb.yaml",
        submit_ready=True,
        workflow_id="wf_03",
        workspace_dir=tmp_path / "workspace" / "wf_03",
        services=deps,
    )

    metadata = stage["metadata"]
    assert stage["status"] == "failed"
    assert stage["task"]["status"] == "failed"
    assert metadata["reaction_handoff_status"] == "failed"
    assert metadata["reaction_handoff_reason"] == "xtb_ts_guess_missing"
    assert metadata["xtb_handoff_retries_used"] == 2
    assert metadata["xtb_handoff_retry_limit"] == 2
    assert stage["output_artifacts"] == []
