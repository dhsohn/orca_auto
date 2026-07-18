from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from orca_auto.flow import orchestration, registry, runtime
from orca_auto.flow.registry import store as registry_store
from tests.flow.orchestration_services import orchestration_services


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


def test_cancel_materialized_workflow_mixes_local_remote_and_failed_cancellations(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_cancel_01",
        "status": "running",
        "stages": [
            {
                "stage_id": "stage_completed",
                "status": "completed",
                "task": {"engine": "crest", "status": "completed"},
            },
            {
                "stage_id": "stage_local",
                "status": "planned",
                "task": {"engine": "crest", "status": "planned"},
            },
            {
                "stage_id": "stage_crest_remote",
                "status": "queued",
                "metadata": {"queue_id": "q_crest"},
                "task": {"engine": "crest", "status": "queued"},
            },
            {
                "stage_id": "stage_xtb_missing_config",
                "status": "running",
                "metadata": {"queue_id": "q_xtb"},
                "task": {"engine": "xtb", "status": "running"},
            },
            {
                "stage_id": "stage_orca_remote",
                "status": "submitted",
                "metadata": {"queue_id": "q_orca"},
                "task": {"engine": "orca", "status": "submitted"},
            },
        ],
    }

    deps = orchestration_services(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: tmp_path / "wf_cancel_01",
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "crest_cancel_target": lambda **kwargs: {
                "status": "cancel_requested",
                "queue_id": kwargs["target"],
            },
            "orca_cancel_target": lambda **kwargs: {
                "status": "cancelled",
                "queue_id": kwargs["target"],
            },
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
        }
    )

    result = orchestration.cancel_materialized_workflow(
        target="wf_cancel_01",
        workflow_root=tmp_path,
        crest_config="/tmp/crest.yaml",
        orca_config="/tmp/orca.yaml",
        services=deps,
    )

    assert result["status"] == "cancel_requested"
    assert result["cancelled"] == [
        {"stage_id": "stage_local", "mode": "local"},
        {"stage_id": "stage_crest_remote", "status": "cancel_requested"},
        {"stage_id": "stage_orca_remote", "status": "cancelled"},
    ]
    assert result["failed"] == [
        {"stage_id": "stage_xtb_missing_config", "reason": "missing_engine_config"},
    ]
    assert payload["stages"][1]["status"] == "cancelled"
    assert payload["stages"][1]["task"]["status"] == "cancelled"
    assert payload["stages"][2]["task"]["cancel_result"]["status"] == "cancel_requested"
    assert payload["stages"][3]["task"]["cancel_result"]["reason"] == "missing_engine_config"
    assert payload["stages"][4]["task"]["cancel_result"]["status"] == "cancelled"


def test_cancel_materialized_workflow_reports_cancelled_when_no_remote_request_pending(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_cancel_02",
        "status": "running",
        "stages": [
            {
                "stage_id": "stage_local",
                "status": "queued",
                "task": {"engine": "crest", "status": "queued"},
            }
        ],
    }

    deps = orchestration_services(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: tmp_path / "wf_cancel_02",
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
        }
    )

    result = orchestration.cancel_materialized_workflow(
        target="wf_cancel_02",
        workflow_root=tmp_path,
        services=deps,
    )

    assert result["status"] == "cancelled"
    assert result["cancelled"] == [{"stage_id": "stage_local", "mode": "local"}]
    assert result["failed"] == []


def test_cancel_materialized_workflow_reports_cancel_failed_when_stage_cancellation_fails(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_failed_cancel",
        "status": "running",
        "stages": [
            {
                "stage_id": "stage_orca_remote",
                "status": "submitted",
                "metadata": {"queue_id": "q_orca"},
                "task": {"engine": "orca", "status": "submitted"},
            },
        ],
    }

    deps = orchestration_services(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / "wf_failed_cancel"
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "orca_cancel_target": lambda **kwargs: {
                "status": "failed",
                "reason": "cancel_command_timeout",
            },
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
        }
    )

    result = orchestration.cancel_materialized_workflow(
        target="wf_failed_cancel",
        workflow_root=tmp_path,
        orca_config="/tmp/orca.yaml",
        services=deps,
    )

    assert result["status"] == "cancel_failed"
    assert result["cancelled"] == []
    assert result["failed"] == [
        {"stage_id": "stage_orca_remote", "reason": "cancel_command_timeout"}
    ]


def test_cancel_materialized_workflow_reports_busy_lock_timeout(
    tmp_path: Path,
) -> None:
    def fake_acquire_workflow_lock(workspace_dir, timeout_seconds=5.0):
        raise TimeoutError("Timed out acquiring lock")

    deps = orchestration_services(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: tmp_path / "workspace",
            "acquire_workflow_lock": fake_acquire_workflow_lock,
        }
    )

    with pytest.raises(
        ValueError, match="Workflow is busy and could not be locked for cancellation within 5s"
    ):
        orchestration.cancel_materialized_workflow(
            target="wf_busy",
            workflow_root=tmp_path,
            services=deps,
        )


def test_cancel_quarantines_identity_mismatch_without_registry_duplicate(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "wf_expected"
    workspace.mkdir()
    payload = {
        "workflow_id": "wf_tampered",
        "template_name": "conformer_screening",
        "status": "running",
        "requested_at": "2026-07-12T00:00:00+00:00",
        "stages": [],
        "metadata": {"si_publish_blocked": True},
    }
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")
    registry_store._save_records(
        tmp_path,
        [
            registry.WorkflowRegistryRecord(
                workflow_id="wf_expected",
                template_name="conformer_screening",
                status="running",
                source_job_id="",
                source_job_type="",
                reaction_key="",
                requested_at="2026-07-12T00:00:00+00:00",
                workspace_dir=str(workspace),
                workflow_file=str(workspace / "workflow.json"),
            )
        ],
    )

    result = orchestration.cancel_materialized_workflow(
        target="wf_expected",
        workflow_root=tmp_path,
    )
    cycle = runtime.advance_workflow_registry_once(
        workflow_root=tmp_path,
        submit_ready=False,
        worker_session_id="post-quarantine-cancel",
        lease_seconds=0,
    )

    assert result["workflow_id"] == "wf_expected"
    assert result["status"] == "failed"
    assert (cycle["advanced_count"], cycle["skipped_count"]) == (0, 1)
    records = registry.list_workflow_registry(tmp_path, reindex_if_missing=False)
    assert [(record.workflow_id, record.status) for record in records] == [
        ("wf_expected", "failed")
    ]
    assert records[0].metadata["quarantined_persisted_workflow_id"] == "wf_tampered"


def test_cancel_clears_stale_identity_error_after_workspace_recovery(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "wf_restored"
    workspace.mkdir()
    payload = {
        "workflow_id": "wf_restored",
        "template_name": "conformer_screening",
        "status": "failed",
        "requested_at": "2026-07-12T00:00:00+00:00",
        "stages": [],
        "metadata": {
            "workflow_error": {
                "status": "failed",
                "scope": "workflow_identity_validation",
                "reason": "old mismatch",
            }
        },
    }
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")

    result = orchestration.cancel_materialized_workflow(
        target="wf_restored",
        workflow_root=tmp_path,
    )

    assert result["status"] == "cancelled"
    persisted = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    assert "workflow_error" not in persisted["metadata"]
    record = registry.get_workflow_registry_record(tmp_path, "wf_restored")
    assert record is not None
    assert "identity_quarantined" not in record.metadata
