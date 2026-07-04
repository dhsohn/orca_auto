from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from orca_auto.flow.orchestration.deps import orchestration_deps
from orca_auto.flow.orchestration.stage_runtime.xtb_sync import sync_xtb_stage_impl


def _write_xyz_ensemble(path: Path, comments: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for comment in comments:
        lines.extend(
            [
                "2",
                comment,
                "H 0 0 0",
                "H 0 0 0.74",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_sync_xtb_stage_submits_initial_attempt_and_records_handoff_metadata(
    tmp_path: Path,
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

    deps = orchestration_deps(
        overrides={
            "_load_config_root": lambda config_path, **kwargs: tmp_path / "xtb_allowed",
            "_ensure_xtb_job_dir": lambda stage, **kwargs: str(
                tmp_path / "xtb_allowed" / "wf_01" / "job_01"
            ),
            "submit_xtb_job_dir": lambda **kwargs: {
                "status": "submitted",
                "queue_id": "q_xtb_01",
                "job_id": "xtb_job_01",
            },
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "_xtb_handoff_status": lambda current_contract: {
                "status": "ready",
                "reason": "",
                "message": "",
                "artifact_path": "/tmp/xtb_done/ts_guess.xyz",
            },
            "now_utc_iso": lambda: "2026-04-19T14:00:00+00:00",
        }
    )

    sync_xtb_stage_impl(
        stage,
        xtb_config="/tmp/xtb.yaml",
        submit_ready=True,
        workflow_id="wf_01",
        workspace_dir=tmp_path / "workspace" / "wf_01",
        deps=deps,
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


def test_sync_xtb_stage_retries_failed_handoff_when_retry_budget_remains(
    tmp_path: Path,
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

    deps = orchestration_deps(
        overrides={
            "_load_config_root": lambda config_path, **kwargs: tmp_path / "xtb_allowed",
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "_xtb_handoff_status": lambda current_contract: {
                "status": "failed",
                "reason": "xtb_ts_guess_missing",
                "message": "missing ts guess",
                "artifact_path": "",
            },
            "_write_xtb_path_job": lambda stage, **kwargs: str(
                tmp_path / "xtb_allowed" / "wf_02" / "retry_attempt_01"
            ),
            "submit_xtb_job_dir": fake_submit_xtb_job_dir,
            "now_utc_iso": lambda: "2026-04-19T14:10:00+00:00",
        }
    )

    sync_xtb_stage_impl(
        stage,
        xtb_config="/tmp/xtb.yaml",
        submit_ready=True,
        workflow_id="wf_02",
        workspace_dir=tmp_path / "workspace" / "wf_02",
        deps=deps,
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
    assert stage["task"]["submission_result"]["queue_id"] == "q_retry_01"
    assert metadata["queue_id"] == "q_retry_01"
    assert metadata["xtb_handoff_status"] == "retrying"
    assert metadata["reaction_handoff_status"] == "retrying"
    assert metadata["xtb_handoff_retries_used"] == 1
    assert metadata["xtb_handoff_retry_limit"] == 2
    assert retry_attempt["submission_status"] == "submitted"
    assert retry_attempt["trigger_reason"] == "xtb_ts_guess_missing"
    assert retry_attempt["trigger_message"] == "missing ts guess"


def test_sync_xtb_stage_stops_retrying_after_limit_and_materializes_empty_candidates(
    tmp_path: Path,
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

    deps = orchestration_deps(
        overrides={
            "_load_config_root": lambda config_path, **kwargs: tmp_path / "xtb_allowed",
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "_xtb_handoff_status": lambda current_contract: {
                "status": "failed",
                "reason": "xtb_ts_guess_missing",
                "message": "missing ts guess",
                "artifact_path": "",
            },
            "submit_xtb_job_dir": lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("should not resubmit once retry limit is exhausted")
            ),
        }
    )

    sync_xtb_stage_impl(
        stage,
        xtb_config="/tmp/xtb.yaml",
        submit_ready=True,
        workflow_id="wf_03",
        workspace_dir=tmp_path / "workspace" / "wf_03",
        deps=deps,
    )

    metadata = stage["metadata"]
    assert stage["status"] == "failed"
    assert stage["task"]["status"] == "failed"
    assert metadata["reaction_handoff_status"] == "failed"
    assert metadata["reaction_handoff_reason"] == "xtb_ts_guess_missing"
    assert metadata["xtb_handoff_retries_used"] == 2
    assert metadata["xtb_handoff_retry_limit"] == 2
    assert stage["output_artifacts"] == []
