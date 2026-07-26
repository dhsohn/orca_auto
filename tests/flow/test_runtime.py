from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.admission import release_slot, reserve_slot
from orca_auto.flow import registry, runtime
from orca_auto.flow.registry import store as registry_store
from orca_auto.flow.stage_transition_events import stage_transition_event_payloads


def _registry_record(
    *,
    workflow_id: str,
    status: str,
    template_name: str = "reaction_ts_search",
    workspace_dir: str = "/tmp/workflow_workspace",
    stage_count: int = 1,
    metadata: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id=workflow_id,
        status=status,
        template_name=template_name,
        workspace_dir=workspace_dir,
        stage_count=stage_count,
        metadata=metadata or {},
    )


def _summary_with_stages(*stages: dict[str, Any]) -> dict[str, Any]:
    return {"stage_summaries": [dict(stage) for stage in stages]}


def _write_workflow_payload(workspace_dir: Path, workflow_id: str) -> None:
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "workflow.json").write_text(
        json.dumps({"workflow_id": workflow_id, "stages": []}),
        encoding="utf-8",
    )


def _capture_worker_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    *,
    records: list[SimpleNamespace],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    state_calls: list[dict[str, Any]] = []
    journal_calls: list[dict[str, Any]] = []
    registry_calls = {"list": 0, "reindex": 0}
    timestamps = iter(
        [
            "2026-04-19T00:00:00+00:00",
            "2026-04-19T00:01:00+00:00",
        ]
    )

    monkeypatch.setattr(runtime, "now_utc_iso", lambda: next(timestamps))

    def fake_append_workflow_journal_event(root: Path, **kwargs: Any) -> None:
        journal_calls.append({"root": root, **kwargs})

    def fake_list_workflow_registry(root: Path) -> list[SimpleNamespace]:
        registry_calls["list"] += 1
        return list(records)

    def fake_reindex_workflow_registry(root: Path) -> list[SimpleNamespace]:
        registry_calls["reindex"] += 1
        return list(records)

    monkeypatch.setattr(
        runtime, "append_workflow_journal_event", fake_append_workflow_journal_event
    )
    monkeypatch.setattr(runtime, "list_workflow_registry", fake_list_workflow_registry)
    monkeypatch.setattr(runtime, "reindex_workflow_registry", fake_reindex_workflow_registry)
    return state_calls, journal_calls, registry_calls


def _always_false_after_append(sync_checks: list[str], workspace_dir: object) -> bool:
    sync_checks.append(str(workspace_dir))
    return False


def test_workflow_worker_lock_path_expands_home_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    lock_path = runtime.workflow_worker_lock_path("~/chem_root")

    assert lock_path == (tmp_path / "chem_root").resolve() / runtime.WORKFLOW_WORKER_LOCK_NAME


def test_stage_transition_event_payloads_emit_start_and_xtb_handoff_events() -> None:
    previous_summary = _summary_with_stages(
        {
            "stage_id": "crest_1",
            "status": "planned",
            "task_status": "planned",
            "engine": "crest",
            "task_kind": "conformer_search",
            "reaction_dir": "/tmp/crest_case",
        },
        {
            "stage_id": "xtb_retry_1",
            "status": "failed",
            "task_status": "failed",
            "engine": "xtb",
            "task_kind": "path_search",
            "reaction_handoff_status": "failed",
            "reaction_handoff_reason": "ts_not_found",
            "xtb_handoff_retries_used": 0,
            "xtb_handoff_retry_limit": 2,
        },
        {
            "stage_id": "xtb_ready_1",
            "status": "running",
            "task_status": "running",
            "engine": "xtb",
            "task_kind": "path_search",
        },
    )
    current_summary = _summary_with_stages(
        {
            "stage_id": "crest_1",
            "status": "queued",
            "task_status": "submitted",
            "submission_status": "submitted",
            "engine": "crest",
            "task_kind": "conformer_search",
            "queue_id": "crest-q-1",
            "reaction_dir": "/tmp/crest_case",
        },
        {
            "stage_id": "xtb_retry_1",
            "status": "queued",
            "task_status": "submitted",
            "engine": "xtb",
            "task_kind": "path_search",
            "queue_id": "xtb-q-1",
            "reaction_handoff_status": "retrying",
            "reaction_handoff_reason": "ts_not_found",
            "xtb_handoff_retries_used": 1,
            "xtb_handoff_retry_limit": 2,
        },
        {
            "stage_id": "xtb_ready_1",
            "status": "completed",
            "task_status": "completed",
            "engine": "xtb",
            "task_kind": "path_search",
            "reaction_handoff_status": "ready",
            "selected_input_xyz": "/tmp/ts_guess.xyz",
        },
    )

    events = stage_transition_event_payloads(
        previous_summary=previous_summary,
        current_summary=current_summary,
        workflow_id="wf_stage_events",
        template_name="reaction_ts_search",
        worker_session_id="session-1",
    )

    assert [item["event_type"] for item in events] == [
        "workflow_stage_submitted",
        "workflow_stage_submitted",
        "workflow_stage_handoff_retrying",
        "workflow_stage_handoff_ready",
    ]
    assert events[0]["metadata"]["stage_id"] == "crest_1"
    assert events[1]["metadata"]["stage_id"] == "xtb_retry_1"
    assert events[1]["metadata"]["xtb_handoff_retries_used"] == 1
    assert events[2]["reason"] == "ts_not_found"
    assert events[3]["status"] == "ready"
    assert all(item["event_type"] != "workflow_stage_completed" for item in events)


def test_stage_transition_event_payloads_emit_completion_and_failure_without_xtb_handoff() -> None:
    previous_summary = _summary_with_stages(
        {
            "stage_id": "crest_done_1",
            "status": "queued",
            "task_status": "submitted",
            "engine": "crest",
            "task_kind": "conformer_search",
        },
        {
            "stage_id": "xtb_submit_fail_1",
            "status": "planned",
            "task_status": "planned",
            "engine": "xtb",
            "task_kind": "path_search",
        },
    )
    current_summary = _summary_with_stages(
        {
            "stage_id": "crest_done_1",
            "status": "completed",
            "task_status": "completed",
            "engine": "crest",
            "task_kind": "conformer_search",
        },
        {
            "stage_id": "xtb_submit_fail_1",
            "status": "submission_failed",
            "task_status": "submission_failed",
            "engine": "xtb",
            "task_kind": "path_search",
            "reason": "submit_failed",
        },
    )

    events = stage_transition_event_payloads(
        previous_summary=previous_summary,
        current_summary=current_summary,
        workflow_id="wf_stage_terminal_events",
        template_name="reaction_ts_search",
        worker_session_id="session-2",
    )

    assert [item["event_type"] for item in events] == [
        "workflow_stage_completed",
        "workflow_stage_failed",
    ]
    assert events[1]["reason"] == "submit_failed"


def test_stage_transition_event_payloads_emit_running_status_change_event() -> None:
    previous_summary = _summary_with_stages(
        {
            "stage_id": "crest_running_1",
            "status": "queued",
            "task_status": "submitted",
            "engine": "crest",
            "task_kind": "conformer_search",
        }
    )
    current_summary = _summary_with_stages(
        {
            "stage_id": "crest_running_1",
            "status": "running",
            "task_status": "running",
            "engine": "crest",
            "task_kind": "conformer_search",
            "queue_id": "crest-q-running",
        }
    )

    events = stage_transition_event_payloads(
        previous_summary=previous_summary,
        current_summary=current_summary,
        workflow_id="wf_stage_running",
        template_name="reaction_ts_search",
        worker_session_id="session-running",
    )

    assert [item["event_type"] for item in events] == ["workflow_stage_status_changed"]
    assert events[0]["stage_status"] == "running"
    assert events[0]["previous_stage_status"] == "queued"


def test_phase_transition_event_payloads_emit_phase_finished_summaries() -> None:
    previous_summary = _summary_with_stages(
        {
            "stage_id": "crest_reactant_01",
            "status": "running",
            "task_status": "running",
            "engine": "crest",
            "task_kind": "conformer_search",
        },
        {
            "stage_id": "crest_product_01",
            "status": "queued",
            "task_status": "submitted",
            "engine": "crest",
            "task_kind": "conformer_search",
        },
        {
            "stage_id": "xtb_path_search_01",
            "status": "running",
            "task_status": "running",
            "engine": "xtb",
            "task_kind": "path_search",
        },
        {
            "stage_id": "xtb_path_search_02",
            "status": "queued",
            "task_status": "submitted",
            "engine": "xtb",
            "task_kind": "path_search",
        },
    )
    current_summary = _summary_with_stages(
        {
            "stage_id": "crest_reactant_01",
            "input_role": "reactant",
            "status": "completed",
            "task_status": "completed",
            "engine": "crest",
            "task_kind": "conformer_search",
        },
        {
            "stage_id": "crest_product_01",
            "input_role": "product",
            "status": "completed",
            "task_status": "completed",
            "engine": "crest",
            "task_kind": "conformer_search",
        },
        {
            "stage_id": "xtb_path_search_01",
            "status": "completed",
            "task_status": "completed",
            "engine": "xtb",
            "task_kind": "path_search",
            "reaction_handoff_status": "ready",
        },
        {
            "stage_id": "xtb_path_search_02",
            "status": "completed",
            "task_status": "completed",
            "engine": "xtb",
            "task_kind": "path_search",
            "reaction_handoff_status": "failed",
            "reaction_handoff_reason": "xtb_ts_guess_missing",
        },
    )

    events = runtime.phase_transition_event_payloads(
        previous_summary=previous_summary,
        current_summary=current_summary,
        workflow_id="wf_phase_events",
        template_name="reaction_ts_search",
        worker_session_id="session-phase",
    )

    assert [item["event_type"] for item in events] == [
        "workflow_phase_finished",
        "workflow_phase_finished",
    ]
    assert events[0]["metadata"]["phase"] == "crest"
    assert events[0]["metadata"]["stage_status_counts"] == {"completed": 2}
    assert events[0]["metadata"]["stage_statuses"] == [
        {
            "stage_id": "crest_reactant_01",
            "label": "reactant",
            "status": "completed",
            "task_status": "completed",
            "result": "completed",
        },
        {
            "stage_id": "crest_product_01",
            "label": "product",
            "status": "completed",
            "task_status": "completed",
            "result": "completed",
        },
    ]
    assert events[1]["metadata"]["phase"] == "xtb"
    assert events[1]["metadata"]["reaction_handoff_status_counts"] == {"failed": 1, "ready": 1}
    assert events[1]["metadata"]["failure_reasons"] == ["xtb_ts_guess_missing"]
    assert [row["result"] for row in events[1]["metadata"]["stage_statuses"]] == [
        "completed",
        "failed",
    ]
    assert events[1]["status"] == "mixed"


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("workflow missing"),
        ValueError("workflow invalid"),
    ],
)
def test_workflow_needs_terminal_sync_returns_false_for_unreadable_payload(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def fake_load_workflow_payload(workspace_dir: str | Path) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(runtime, "load_workflow_payload", fake_load_workflow_payload)
    monkeypatch.setattr(
        runtime,
        "workflow_has_active_downstream",
        lambda payload: pytest.fail("downstream activity should not be consulted"),
    )

    assert runtime._workflow_needs_terminal_sync("/tmp/workflow_workspace") is False


def test_workflow_needs_terminal_sync_short_circuits_for_final_child_sync_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "metadata": {"final_child_sync_pending": True},
        "stages": [],
    }

    monkeypatch.setattr(runtime, "load_workflow_payload", lambda workspace_dir: payload)
    monkeypatch.setattr(
        runtime,
        "workflow_has_active_downstream",
        lambda payload: pytest.fail("downstream activity should not be consulted"),
    )

    assert runtime._workflow_needs_terminal_sync("/tmp/workflow_workspace") is True


def test_workflow_needs_terminal_sync_retries_pending_si_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"metadata": {"si_publish_pending": True}, "stages": []}
    monkeypatch.setattr(runtime, "load_workflow_payload", lambda workspace_dir: payload)
    monkeypatch.setattr(
        runtime,
        "workflow_has_active_downstream",
        lambda payload: pytest.fail("downstream activity should not be consulted"),
    )
    assert runtime._workflow_needs_terminal_sync("/tmp/workflow_workspace") is True


def test_si_publish_retry_due_respects_backoff_and_blocked_state() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    assert (
        runtime.si_publish_retry_due(
            {"si_publish_pending": True, "si_publish_next_retry_at": "2026-07-12T00:00:01+00:00"},
            now=now,
        )
        is False
    )
    assert (
        runtime.si_publish_retry_due(
            {"si_publish_pending": True, "si_publish_next_retry_at": "2026-07-11T23:59:59+00:00"},
            now=now,
        )
        is True
    )
    assert (
        runtime.si_publish_retry_due(
            {"si_publish_pending": True, "si_publish_blocked": True},
            now=now,
        )
        is False
    )


def test_terminal_sync_reconciles_stale_registry_pending_after_payload_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _registry_record(
        workflow_id="wf_stale_registry",
        status="completed",
        metadata={"si_publish_pending": True},
    )
    monkeypatch.setattr(
        runtime,
        "load_workflow_payload",
        lambda workspace_dir: {"metadata": {"si_publish_pending": False}, "stages": []},
    )
    monkeypatch.setattr(runtime, "workflow_has_active_downstream", lambda payload: False)
    assert runtime._workflow_needs_terminal_child_sync(
        record,
        previous_status="completed",
    )


def test_terminal_sync_reconciles_stale_failed_registry_after_authoritative_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _registry_record(
        workflow_id="wf_restarted",
        status="failed",
        workspace_dir="/tmp/wf_restarted",
    )
    monkeypatch.setattr(
        runtime,
        "load_workflow_payload",
        lambda workspace_dir: {
            "workflow_id": "wf_restarted",
            "status": "planned",
            "metadata": {},
            "stages": [],
        },
    )
    monkeypatch.setattr(runtime, "workflow_has_active_downstream", lambda payload: False)

    assert runtime._workflow_needs_terminal_child_sync(
        record,
        previous_status="failed",
    )


def test_terminal_sync_reconciles_authoritative_blocked_payload_with_stale_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _registry_record(
        workflow_id="wf_blocked",
        status="completed",
        metadata={"si_publish_pending": True},
    )
    monkeypatch.setattr(
        runtime,
        "load_workflow_payload",
        lambda workspace_dir: {
            "metadata": {"si_publish_blocked": True, "si_publish_pending": False},
            "stages": [],
        },
    )
    monkeypatch.setattr(runtime, "workflow_has_active_downstream", lambda payload: False)
    assert runtime._workflow_needs_terminal_child_sync(
        record,
        previous_status="completed",
    )


def test_authoritative_blocked_payload_skips_when_registry_is_already_reconciled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _registry_record(
        workflow_id="wf_blocked",
        status="completed",
        workspace_dir="/tmp/wf_blocked",
        metadata={"si_publish_blocked": True, "si_publish_pending": False},
    )
    monkeypatch.setattr(
        runtime,
        "load_workflow_payload",
        lambda workspace_dir: {
            "workflow_id": "wf_blocked",
            "status": "completed",
            "metadata": {"si_publish_blocked": True, "si_publish_pending": False},
            "stages": [],
        },
    )
    monkeypatch.setattr(runtime, "workflow_has_active_downstream", lambda payload: False)

    assert not runtime._workflow_needs_terminal_child_sync(
        record,
        previous_status="completed",
    )


def test_quarantined_identity_mismatch_does_not_hot_loop_terminal_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _registry_record(
        workflow_id="wf_expected",
        status="failed",
        workspace_dir="/tmp/wf_expected",
        metadata={"quarantined_persisted_workflow_id": "wf_tampered"},
    )
    monkeypatch.setattr(
        runtime,
        "load_workflow_payload",
        lambda workspace_dir: {
            "workflow_id": "wf_tampered",
            "status": "failed",
            "metadata": {
                "workflow_error": {
                    "status": "failed",
                    "scope": "workflow_identity_validation",
                }
            },
            "stages": [],
        },
    )
    monkeypatch.setattr(runtime, "workflow_has_active_downstream", lambda payload: False)

    assert not runtime._workflow_needs_terminal_child_sync(
        record,
        previous_status="failed",
    )


def test_quarantined_identity_reconciles_stale_cached_status_and_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _registry_record(workflow_id="wf_expected", status="completed")
    monkeypatch.setattr(
        runtime,
        "load_workflow_payload",
        lambda workspace_dir: {
            "workflow_id": "wf_tampered",
            "status": "failed",
            "metadata": {
                "workflow_error": {
                    "status": "failed",
                    "scope": "workflow_identity_validation",
                }
            },
            "stages": [],
        },
    )
    monkeypatch.setattr(runtime, "workflow_has_active_downstream", lambda payload: False)

    assert runtime._workflow_needs_terminal_child_sync(
        record,
        previous_status="completed",
    )


def test_restored_identity_reconciles_stale_cached_quarantine_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _registry_record(
        workflow_id="wf_restored",
        status="failed",
        workspace_dir="/tmp/wf_restored",
        metadata={
            "si_publish_blocked": True,
            "quarantined_persisted_workflow_id": "wf_tampered",
        },
    )
    monkeypatch.setattr(
        runtime,
        "load_workflow_payload",
        lambda workspace_dir: {
            "workflow_id": "wf_restored",
            "status": "failed",
            "metadata": {"si_publish_blocked": True},
            "stages": [],
        },
    )
    monkeypatch.setattr(runtime, "workflow_has_active_downstream", lambda payload: False)

    assert runtime._workflow_needs_terminal_child_sync(
        record,
        previous_status="failed",
    )


def test_quarantined_identity_reconciles_stale_blocked_registry_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _registry_record(
        workflow_id="wf_expected",
        status="failed",
        metadata={"si_publish_pending": True},
    )
    monkeypatch.setattr(
        runtime,
        "load_workflow_payload",
        lambda workspace_dir: {
            "workflow_id": "wf_tampered",
            "status": "failed",
            "metadata": {
                "si_publish_pending": False,
                "si_publish_blocked": True,
                "workflow_error": {
                    "status": "failed",
                    "scope": "workflow_identity_validation",
                },
            },
            "stages": [],
        },
    )
    monkeypatch.setattr(runtime, "workflow_has_active_downstream", lambda payload: False)

    assert runtime._workflow_needs_terminal_child_sync(
        record,
        previous_status="failed",
    )


def test_terminal_sync_reconciles_cleared_authoritative_child_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _registry_record(
        workflow_id="wf_child_synced",
        status="failed",
        metadata={"final_child_sync_pending": True},
    )
    monkeypatch.setattr(
        runtime,
        "load_workflow_payload",
        lambda workspace_dir: {
            "workflow_id": "wf_tampered",
            "status": "failed",
            "metadata": {
                "final_child_sync_pending": False,
                "workflow_error": {
                    "status": "failed",
                    "scope": "workflow_identity_validation",
                },
            },
            "stages": [],
        },
    )
    monkeypatch.setattr(runtime, "workflow_has_active_downstream", lambda payload: False)

    assert runtime._workflow_needs_terminal_child_sync(
        record,
        previous_status="failed",
    )


def test_terminal_sync_reconciles_quarantined_stale_si_pending_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _registry_record(
        workflow_id="wf_si_synced",
        status="failed",
        metadata={"si_publish_pending": True},
    )
    monkeypatch.setattr(
        runtime,
        "load_workflow_payload",
        lambda workspace_dir: {
            "workflow_id": "wf_tampered",
            "status": "failed",
            "metadata": {
                "si_publish_pending": False,
                "workflow_error": {
                    "status": "failed",
                    "scope": "workflow_identity_validation",
                },
            },
            "stages": [],
        },
    )
    monkeypatch.setattr(runtime, "workflow_has_active_downstream", lambda payload: False)

    assert runtime._workflow_needs_terminal_child_sync(
        record,
        previous_status="failed",
    )


def test_identity_quarantine_replaces_stale_active_row_and_stays_idle(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workspace = workflow_root / "wf_expected"
    workspace.mkdir(parents=True)
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
        workflow_root,
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

    first = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        worker_session_id="quarantine-cycle-1",
        lease_seconds=0,
    )
    second = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        worker_session_id="quarantine-cycle-2",
        lease_seconds=0,
    )
    refreshed = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        refresh_registry=True,
        worker_session_id="quarantine-cycle-3",
        lease_seconds=0,
    )

    assert (first["advanced_count"], first["skipped_count"]) == (1, 0)
    assert (second["advanced_count"], second["skipped_count"]) == (0, 1)
    assert (refreshed["advanced_count"], refreshed["skipped_count"]) == (0, 1)
    records = registry.list_workflow_registry(workflow_root, reindex_if_missing=False)
    assert [(record.workflow_id, record.status) for record in records] == [
        ("wf_expected", "failed")
    ]
    assert records[0].metadata["quarantined_persisted_workflow_id"] == "wf_tampered"


def test_reindexed_terminal_identity_mismatch_is_quarantined_once(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workspace = workflow_root / "wf_expected"
    workspace.mkdir(parents=True)
    payload = {
        "workflow_id": "wf_tampered",
        "template_name": "conformer_screening",
        "status": "cancelled",
        "requested_at": "2026-07-12T00:00:00+00:00",
        "stages": [],
        "metadata": {"si_publish_blocked": True},
    }
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")

    first = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        refresh_registry=True,
        worker_session_id="reindexed-quarantine-1",
        lease_seconds=0,
    )
    second = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        worker_session_id="reindexed-quarantine-2",
        lease_seconds=0,
    )

    assert (first["advanced_count"], first["skipped_count"]) == (1, 0)
    assert (second["advanced_count"], second["skipped_count"]) == (0, 1)
    records = registry.list_workflow_registry(workflow_root, reindex_if_missing=False)
    assert [(record.workflow_id, record.status) for record in records] == [
        ("wf_expected", "failed")
    ]
    assert records[0].metadata["quarantined_persisted_workflow_id"] == "wf_tampered"


def test_quarantined_identity_uses_current_workspace_when_cached_path_is_stale(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workspace = workflow_root / "wf_expected"
    workspace.mkdir(parents=True)
    payload = {
        "workflow_id": "wf_tampered",
        "template_name": "conformer_screening",
        "status": "failed",
        "requested_at": "2026-07-12T00:00:00+00:00",
        "stages": [],
        "metadata": {
            "si_publish_blocked": True,
            "workflow_error": {
                "status": "failed",
                "scope": "workflow_identity_validation",
            },
        },
    }
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")
    registry_store._save_records(
        workflow_root,
        [
            registry.WorkflowRegistryRecord(
                workflow_id="wf_expected",
                template_name="conformer_screening",
                status="failed",
                source_job_id="",
                source_job_type="",
                reaction_key="",
                requested_at="2026-07-12T00:00:00+00:00",
                workspace_dir=str(workflow_root / "old_missing"),
                workflow_file=str(workflow_root / "old_missing" / "workflow.json"),
                metadata={
                    "si_publish_blocked": True,
                    "quarantined_persisted_workflow_id": "wf_tampered",
                },
            )
        ],
    )

    first = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        worker_session_id="quarantine-stale-path-1",
        lease_seconds=0,
    )
    second = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        worker_session_id="quarantine-stale-path-2",
        lease_seconds=0,
    )

    assert (first["advanced_count"], first["failed_count"]) == (1, 0)
    assert (second["advanced_count"], second["skipped_count"]) == (0, 1)
    record = registry.list_workflow_registry(workflow_root, reindex_if_missing=False)[0]
    assert Path(record.workspace_dir) == workspace


def test_quarantined_identity_repairs_wrong_cached_registry_id_once(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workspace = workflow_root / "wf_expected"
    workspace.mkdir(parents=True)
    payload = {
        "workflow_id": "wf_tampered",
        "template_name": "conformer_screening",
        "status": "failed",
        "requested_at": "2026-07-12T00:00:00+00:00",
        "stages": [],
        "metadata": {
            "si_publish_blocked": True,
            "workflow_error": {
                "status": "failed",
                "scope": "workflow_identity_validation",
            },
        },
    }
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")
    registry_store._save_records(
        workflow_root,
        [
            registry.WorkflowRegistryRecord(
                workflow_id="wf_wrong",
                template_name="conformer_screening",
                status="failed",
                source_job_id="",
                source_job_type="",
                reaction_key="",
                requested_at="2026-07-12T00:00:00+00:00",
                workspace_dir=str(workspace),
                workflow_file=str(workspace / "workflow.json"),
                metadata={
                    "si_publish_blocked": True,
                    "identity_quarantined": True,
                    "quarantined_persisted_workflow_id": "wf_tampered",
                },
            )
        ],
    )

    first = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        worker_session_id="quarantine-wrong-id-1",
        lease_seconds=0,
    )
    second = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        worker_session_id="quarantine-wrong-id-2",
        lease_seconds=0,
    )

    assert (first["advanced_count"], first["failed_count"]) == (1, 0)
    assert (second["advanced_count"], second["skipped_count"]) == (0, 1)
    records = registry.list_workflow_registry(workflow_root, reindex_if_missing=False)
    assert [(record.workflow_id, record.status) for record in records] == [
        ("wf_expected", "failed")
    ]


def test_prequarantine_identity_uses_current_workspace_when_cached_path_is_stale(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workspace = workflow_root / "wf_expected"
    workspace.mkdir(parents=True)
    payload = {
        "workflow_id": "wf_tampered",
        "template_name": "conformer_screening",
        "status": "completed",
        "requested_at": "2026-07-12T00:00:00+00:00",
        "stages": [],
        "metadata": {"si_publish_blocked": True},
    }
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")
    registry_store._save_records(
        workflow_root,
        [
            registry.WorkflowRegistryRecord(
                workflow_id="wf_expected",
                template_name="conformer_screening",
                status="completed",
                source_job_id="",
                source_job_type="",
                reaction_key="",
                requested_at="2026-07-12T00:00:00+00:00",
                workspace_dir=str(workflow_root / "old_missing"),
                workflow_file=str(workflow_root / "old_missing" / "workflow.json"),
                metadata={
                    "si_publish_blocked": True,
                    "identity_reconciliation_required": True,
                    "identity_reconciliation_persisted_workflow_id": "wf_tampered",
                },
            )
        ],
    )

    first = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        worker_session_id="reconciliation-stale-path-1",
        lease_seconds=0,
    )
    second = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        worker_session_id="reconciliation-stale-path-2",
        lease_seconds=0,
    )

    assert (first["advanced_count"], first["failed_count"]) == (1, 0)
    assert (second["advanced_count"], second["skipped_count"]) == (0, 1)
    record = registry.list_workflow_registry(workflow_root, reindex_if_missing=False)[0]
    assert record.workflow_id == "wf_expected"
    assert Path(record.workspace_dir) == workspace
    assert record.metadata["identity_quarantined"] is True


def test_missing_stale_terminal_workspace_stays_visible_without_hot_loop(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workflow_root.mkdir()
    registry_store._save_records(
        workflow_root,
        [
            registry.WorkflowRegistryRecord(
                workflow_id="wf_expected",
                template_name="conformer_screening",
                status="completed",
                source_job_id="",
                source_job_type="",
                reaction_key="",
                requested_at="2026-07-12T00:00:00+00:00",
                workspace_dir=str(workflow_root / "old_missing"),
                workflow_file=str(workflow_root / "old_missing" / "workflow.json"),
            )
        ],
    )

    first = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        worker_session_id="missing-terminal-1",
        lease_seconds=0,
    )
    second = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        worker_session_id="missing-terminal-2",
        lease_seconds=0,
    )

    assert (first["advanced_count"], first["skipped_count"], first["failed_count"]) == (
        0,
        1,
        0,
    )
    assert (second["advanced_count"], second["skipped_count"], second["failed_count"]) == (
        0,
        1,
        0,
    )


def test_restored_workspace_identity_clears_stale_quarantine_once(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workspace = workflow_root / "wf_restored"
    workspace.mkdir(parents=True)
    payload = {
        "template_name": "conformer_screening",
        "status": "failed",
        "requested_at": "2026-07-12T00:00:00+00:00",
        "stages": [],
        "metadata": {
            "si_publish_blocked": True,
            "workflow_error": {
                "status": "failed",
                "scope": "workflow_identity_validation",
                "reason": "old mismatch",
                "message": "old mismatch",
            },
        },
    }
    payload["workflow_id"] = "wf_restored"
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")

    first = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        refresh_registry=True,
        worker_session_id="restored-identity-1",
        lease_seconds=0,
    )
    second = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        worker_session_id="restored-identity-2",
        lease_seconds=0,
    )

    assert (first["advanced_count"], first["failed_count"]) == (1, 0)
    assert (second["advanced_count"], second["skipped_count"]) == (0, 1)
    persisted = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    assert "workflow_error" not in persisted["metadata"]
    records = registry.list_workflow_registry(workflow_root, reindex_if_missing=False)
    assert [(record.workflow_id, record.status) for record in records] == [
        ("wf_restored", "failed")
    ]
    assert "quarantined_persisted_workflow_id" not in records[0].metadata


@pytest.mark.parametrize("persisted_id", ["wf(bad)"])
def test_invalid_workspace_segment_quarantines_then_idles(
    tmp_path: Path,
    persisted_id: str | None,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workspace = workflow_root / "wf(bad)"
    workspace.mkdir(parents=True)
    payload = {
        "template_name": "conformer_screening",
        "status": "completed",
        "requested_at": "2026-07-12T00:00:00+00:00",
        "stages": [],
        "metadata": {"si_publish_blocked": True},
    }
    if persisted_id is not None:
        payload["workflow_id"] = persisted_id
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")

    first = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        refresh_registry=True,
        worker_session_id="invalid-segment-1",
        lease_seconds=0,
    )
    second = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        worker_session_id="invalid-segment-2",
        lease_seconds=0,
    )

    assert (first["advanced_count"], first["failed_count"]) == (1, 0)
    assert (second["advanced_count"], second["skipped_count"]) == (0, 1)
    records = registry.list_workflow_registry(workflow_root, reindex_if_missing=False)
    assert len(records) == 1
    assert records[0].metadata["identity_quarantined"] is True
    assert records[0].metadata["quarantined_persisted_workflow_id"] == (persisted_id or "")


def test_previously_cleared_identity_mismatch_is_reindexed_and_quarantined(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workspace = workflow_root / "wf_expected"
    workspace.mkdir(parents=True)
    valid_payload = {
        "workflow_id": "wf_expected",
        "template_name": "conformer_screening",
        "status": "completed",
        "requested_at": "2026-07-12T00:00:00+00:00",
        "stages": [],
        "metadata": {},
    }
    (workspace / "workflow.json").write_text(json.dumps(valid_payload), encoding="utf-8")
    registry.sync_workflow_registry(workflow_root, workspace, valid_payload)
    assert registry.clear_terminal_workflow_registry(workflow_root) == 1

    tampered_payload = {**valid_payload, "workflow_id": "wf_tampered"}
    (workspace / "workflow.json").write_text(json.dumps(tampered_payload), encoding="utf-8")
    first = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        refresh_registry=True,
        worker_session_id="cleared-mismatch-1",
        lease_seconds=0,
    )
    second = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=False,
        worker_session_id="cleared-mismatch-2",
        lease_seconds=0,
    )

    assert first["discovered_count"] == 1
    assert (first["advanced_count"], first["failed_count"]) == (1, 0)
    assert (second["advanced_count"], second["skipped_count"]) == (0, 1)
    records = registry.list_workflow_registry(workflow_root, reindex_if_missing=False)
    assert [(record.workflow_id, record.status) for record in records] == [
        ("wf_expected", "failed")
    ]
    assert records[0].metadata["quarantined_persisted_workflow_id"] == "wf_tampered"


@pytest.mark.parametrize(
    "stage",
    [
        {"status": " running ", "task": {"status": "completed"}},
        {"status": "completed", "task": {"status": " Submitted "}},
    ],
)
def test_workflow_needs_terminal_sync_detects_active_stage_or_task_status(
    monkeypatch: pytest.MonkeyPatch,
    stage: dict[str, Any],
) -> None:
    monkeypatch.setattr(
        runtime,
        "load_workflow_payload",
        lambda workspace_dir: {"metadata": {}, "stages": [stage]},
    )
    monkeypatch.setattr(
        runtime,
        "workflow_has_active_downstream",
        lambda payload: pytest.fail("downstream activity should not be consulted"),
    )

    assert runtime._workflow_needs_terminal_sync("/tmp/workflow_workspace") is True


@pytest.mark.parametrize(("downstream_active", "expected"), [(True, True), (False, False)])
def test_workflow_needs_terminal_sync_falls_back_to_downstream_activity(
    monkeypatch: pytest.MonkeyPatch,
    downstream_active: bool,
    expected: bool,
) -> None:
    payload = {
        "metadata": {},
        "stages": [{"status": "completed", "task": {"status": "completed"}}],
    }
    downstream_checks: list[dict[str, Any]] = []

    monkeypatch.setattr(runtime, "load_workflow_payload", lambda workspace_dir: payload)

    def fake_workflow_has_active_downstream(current_payload: dict[str, Any]) -> bool:
        downstream_checks.append(current_payload)
        return downstream_active

    monkeypatch.setattr(
        runtime, "workflow_has_active_downstream", fake_workflow_has_active_downstream
    )

    assert runtime._workflow_needs_terminal_sync("/tmp/workflow_workspace") is expected
    assert downstream_checks == [payload]


def test_advance_workflow_registry_once_skips_terminal_workflow_without_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record = _registry_record(
        workflow_id="wf_terminal_skip",
        status="completed",
        workspace_dir="/tmp/wf_terminal_skip",
        stage_count=2,
    )
    state_calls, journal_calls, registry_calls = _capture_worker_side_effects(
        monkeypatch,
        records=[record],
    )
    sync_checks: list[str] = []

    monkeypatch.setattr(
        runtime,
        "_workflow_needs_terminal_sync",
        lambda workspace_dir: _always_false_after_append(sync_checks, workspace_dir),
    )
    monkeypatch.setattr(
        runtime,
        "advance_workflow",
        lambda **kwargs: pytest.fail(
            "advance_workflow should not run for skipped terminal workflows"
        ),
    )

    result = runtime.advance_workflow_registry_once(
        workflow_root=tmp_path / "workflow_root",
        worker_session_id="session-1",
        lease_seconds=0,
    )

    assert result["workflow_root"] == str((tmp_path / "workflow_root").resolve())
    assert result["discovered_count"] == 1
    assert result["advanced_count"] == 0
    assert result["skipped_count"] == 1
    assert result["failed_count"] == 0
    assert result["workflow_results"] == [
        {
            "workflow_id": "wf_terminal_skip",
            "template_name": "reaction_ts_search",
            "previous_status": "completed",
            "status": "completed",
            "advanced": False,
            "reason": "terminal_status",
            "stage_count": 2,
        }
    ]
    assert sync_checks == ["/tmp/wf_terminal_skip"]
    assert registry_calls == {"list": 1, "reindex": 0}
    assert state_calls == []
    assert journal_calls == []


def test_advance_workflow_registry_once_runs_terminal_child_sync_when_needed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record = _registry_record(
        workflow_id="wf_terminal_sync",
        status="failed",
        workspace_dir="/tmp/wf_terminal_sync",
        stage_count=1,
    )
    state_calls, journal_calls, registry_calls = _capture_worker_side_effects(
        monkeypatch,
        records=[record],
    )
    advance_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(runtime, "_workflow_needs_terminal_sync", lambda workspace_dir: True)

    def fake_advance_workflow(**kwargs: Any) -> dict[str, Any]:
        advance_calls.append(kwargs)
        return {
            "workflow_id": "wf_terminal_sync",
            "template_name": "reaction_ts_search",
            "status": "completed",
            "stages": [{"stage_id": "s1"}, {"stage_id": "s2"}],
        }

    monkeypatch.setattr(runtime, "advance_workflow", fake_advance_workflow)

    result = runtime.advance_workflow_registry_once(
        workflow_root=tmp_path / "workflow_root",
        submit_ready=True,
        worker_session_id="session-1",
        lease_seconds=0,
    )

    assert result["advanced_count"] == 1
    assert result["skipped_count"] == 0
    assert result["failed_count"] == 0
    assert registry_calls == {"list": 1, "reindex": 0}
    assert advance_calls[0]["target"] == "/tmp/wf_terminal_sync"
    assert advance_calls[0]["submit_ready"] is False
    assert result["workflow_results"] == [
        {
            "workflow_id": "wf_terminal_sync",
            "template_name": "reaction_ts_search",
            "previous_status": "failed",
            "status": "completed",
            "advanced": True,
            "changed": True,
            "reason": "terminal_child_sync",
            "stage_count": 2,
        }
    ]
    assert state_calls == []
    assert [call["event_type"] for call in journal_calls] == ["workflow_status_changed"]
    assert journal_calls[0]["reason"] == "terminal_child_sync"


def test_advance_workflow_registry_once_advances_non_terminal_workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record = _registry_record(
        workflow_id="wf_running",
        status="queued",
        workspace_dir="/tmp/wf_running",
        stage_count=1,
    )
    _, journal_calls, registry_calls = _capture_worker_side_effects(
        monkeypatch,
        records=[record],
    )
    advance_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        runtime,
        "_workflow_needs_terminal_sync",
        lambda workspace_dir: pytest.fail(
            "terminal sync checks should not run for active workflows"
        ),
    )

    def fake_advance_workflow(**kwargs: Any) -> dict[str, Any]:
        advance_calls.append(kwargs)
        return {
            "workflow_id": "wf_running",
            "template_name": "reaction_ts_search",
            "status": "running",
            "stages": [{"stage_id": "s1"}, {"stage_id": "s2"}, {"stage_id": "s3"}],
        }

    monkeypatch.setattr(runtime, "advance_workflow", fake_advance_workflow)

    result = runtime.advance_workflow_registry_once(
        workflow_root=tmp_path / "workflow_root",
        refresh_registry=True,
        submit_ready=False,
        worker_session_id="session-1",
        lease_seconds=0,
    )

    assert result["advanced_count"] == 1
    assert result["skipped_count"] == 0
    assert result["failed_count"] == 0
    assert registry_calls == {"list": 0, "reindex": 1}
    assert advance_calls[0]["submit_ready"] is False
    assert result["workflow_results"] == [
        {
            "workflow_id": "wf_running",
            "template_name": "reaction_ts_search",
            "previous_status": "queued",
            "status": "running",
            "advanced": True,
            "changed": True,
            "stage_count": 3,
        }
    ]
    assert [call["event_type"] for call in journal_calls] == ["workflow_status_changed"]


def test_registry_worker_falls_back_to_workflow_id_when_workspace_path_is_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    current_workspace = workflow_root / "wf_moved"
    _write_workflow_payload(current_workspace, "wf_moved")
    stale_workspace = workflow_root / "stale_workspace"
    stale_workspace.mkdir()
    record = _registry_record(
        workflow_id="wf_moved",
        status="running",
        workspace_dir=str(stale_workspace),
    )
    _capture_worker_side_effects(monkeypatch, records=[record])
    advance_calls: list[dict[str, Any]] = []
    summary_paths: list[str] = []

    monkeypatch.setattr(
        runtime,
        "_workflow_needs_terminal_sync",
        lambda workspace_dir: pytest.fail(
            "terminal sync checks should not run for active workflows"
        ),
    )

    def fake_summary(workspace_dir: str | Path, **kwargs: Any) -> dict[str, Any]:
        summary_paths.append(str(workspace_dir))
        return {}

    def fake_advance_workflow(**kwargs: Any) -> dict[str, Any]:
        advance_calls.append(kwargs)
        return {
            "workflow_id": "wf_moved",
            "template_name": "reaction_ts_search",
            "status": "running",
            "stages": [],
        }

    monkeypatch.setattr(runtime, "_safe_workflow_summary", fake_summary)
    monkeypatch.setattr(runtime, "advance_workflow", fake_advance_workflow)

    result = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        worker_session_id="session-moved",
        lease_seconds=0,
    )

    assert result["advanced_count"] == 1
    assert advance_calls[0]["target"] == "wf_moved"
    assert summary_paths == [str(current_workspace.resolve())] * 2


def test_registry_worker_ignores_stale_workspace_copy_with_matching_workflow_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    current_workspace = workflow_root / "wf_copied"
    _write_workflow_payload(current_workspace, "wf_copied")
    stale_workspace = workflow_root / "stale_workspace_copy"
    _write_workflow_payload(stale_workspace, "wf_copied")
    record = _registry_record(
        workflow_id="wf_copied",
        status="running",
        workspace_dir=str(stale_workspace),
    )
    _capture_worker_side_effects(monkeypatch, records=[record])
    advance_calls: list[dict[str, Any]] = []
    summary_paths: list[str] = []

    monkeypatch.setattr(
        runtime,
        "_workflow_needs_terminal_sync",
        lambda workspace_dir: pytest.fail(
            "terminal sync checks should not run for active workflows"
        ),
    )

    def fake_summary(workspace_dir: str | Path, **kwargs: Any) -> dict[str, Any]:
        summary_paths.append(str(workspace_dir))
        return {}

    def fake_advance_workflow(**kwargs: Any) -> dict[str, Any]:
        advance_calls.append(kwargs)
        return {
            "workflow_id": "wf_copied",
            "template_name": "reaction_ts_search",
            "status": "running",
            "stages": [],
        }

    monkeypatch.setattr(runtime, "_safe_workflow_summary", fake_summary)
    monkeypatch.setattr(runtime, "advance_workflow", fake_advance_workflow)

    result = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        worker_session_id="session-copied",
        lease_seconds=0,
    )

    assert result["advanced_count"] == 1
    assert advance_calls[0]["target"] == "wf_copied"
    assert summary_paths == [str(current_workspace.resolve())] * 2


def test_stale_registry_path_uses_current_workspace_for_terminal_child_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflow_root"
    current_workspace = workflow_root / "wf_terminal_moved"
    _write_workflow_payload(current_workspace, "wf_terminal_moved")
    stale_workspace = workflow_root / "stale_terminal_workspace"
    stale_workspace.mkdir()
    record = _registry_record(
        workflow_id="wf_terminal_moved",
        status="failed",
        workspace_dir=str(stale_workspace),
    )
    _capture_worker_side_effects(monkeypatch, records=[record])
    sync_checks: list[str] = []
    advance_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        runtime,
        "_workflow_needs_terminal_sync",
        lambda workspace_dir: _always_false_after_append(sync_checks, workspace_dir) or True,
    )

    def fake_advance_workflow(**kwargs: Any) -> dict[str, Any]:
        advance_calls.append(kwargs)
        return {
            "workflow_id": "wf_terminal_moved",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "stages": [],
        }

    monkeypatch.setattr(runtime, "advance_workflow", fake_advance_workflow)

    result = runtime.advance_workflow_registry_once(
        workflow_root=workflow_root,
        submit_ready=True,
        worker_session_id="session-terminal-moved",
        lease_seconds=0,
    )

    assert sync_checks == [str(current_workspace.resolve())]
    assert advance_calls[0]["target"] == "wf_terminal_moved"
    assert advance_calls[0]["submit_ready"] is False
    assert result["workflow_results"][0]["reason"] == "terminal_child_sync"


def test_advance_workflow_registry_once_defers_submission_when_admission_full(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record = _registry_record(
        workflow_id="wf_waiting",
        status="queued",
        workspace_dir="/tmp/wf_waiting",
        stage_count=1,
    )
    state_calls, journal_calls, _registry_calls = _capture_worker_side_effects(
        monkeypatch,
        records=[record],
    )
    advance_calls: list[dict[str, Any]] = []
    admission_root = tmp_path / "admission"
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        "\n".join(
            [
                "scheduler:",
                "  max_active_simulations: 1",
                f"  admission_root: {admission_root}",
                "workflow:",
                f"  root: {tmp_path / 'workflow_root'}",
            ]
        ),
        encoding="utf-8",
    )

    token = reserve_slot(admission_root, 1, source="test")
    assert token is not None

    monkeypatch.setattr(
        runtime,
        "_workflow_needs_terminal_sync",
        lambda workspace_dir: pytest.fail(
            "terminal sync checks should not run for active workflows"
        ),
    )

    def fake_advance_workflow(**kwargs: Any) -> dict[str, Any]:
        advance_calls.append(kwargs)
        return {
            "workflow_id": "wf_waiting",
            "template_name": "reaction_ts_search",
            "status": "running",
            "stages": [{"stage_id": "s1"}],
        }

    monkeypatch.setattr(runtime, "advance_workflow", fake_advance_workflow)

    try:
        result = runtime.advance_workflow_registry_once(
            workflow_root=tmp_path / "workflow_root",
            orca_config=str(config_path),
            submit_ready=True,
            worker_session_id="session-1",
            lease_seconds=0,
        )
    finally:
        release_slot(admission_root, token)

    assert advance_calls[0]["submit_ready"] is False
    assert result["submit_ready"] is False
    assert result["requested_submit_ready"] is True
    assert result["admission_blocked"] is True
    assert state_calls == []
    assert all(call["event_type"] != "worker_cycle_started" for call in journal_calls)


def test_advance_workflow_registry_once_defers_submission_when_admission_check_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record = _registry_record(
        workflow_id="wf_waiting",
        status="queued",
        workspace_dir="/tmp/wf_waiting",
        stage_count=1,
    )
    state_calls, journal_calls, _registry_calls = _capture_worker_side_effects(
        monkeypatch,
        records=[record],
    )
    advance_calls: list[dict[str, Any]] = []
    admission_root = tmp_path / "admission"
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        "\n".join(
            [
                "scheduler:",
                "  max_active_simulations: 1",
                f"  admission_root: {admission_root}",
                "workflow:",
                f"  root: {tmp_path / 'workflow_root'}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runtime,
        "active_slot_count",
        lambda _admission_root: (_ for _ in ()).throw(OSError("slot store unavailable")),
    )
    monkeypatch.setattr(
        runtime,
        "_workflow_needs_terminal_sync",
        lambda workspace_dir: pytest.fail(
            "terminal sync checks should not run for active workflows"
        ),
    )

    def fake_advance_workflow(**kwargs: Any) -> dict[str, Any]:
        advance_calls.append(kwargs)
        return {
            "workflow_id": "wf_waiting",
            "template_name": "reaction_ts_search",
            "status": "running",
            "stages": [{"stage_id": "s1"}],
        }

    monkeypatch.setattr(runtime, "advance_workflow", fake_advance_workflow)

    result = runtime.advance_workflow_registry_once(
        workflow_root=tmp_path / "workflow_root",
        orca_config=str(config_path),
        submit_ready=True,
        worker_session_id="session-1",
        lease_seconds=0,
    )

    assert advance_calls[0]["submit_ready"] is False
    assert result["submit_ready"] is False
    assert result["requested_submit_ready"] is True
    assert result["admission_blocked"] is True
    assert state_calls == []
    assert all(call["event_type"] != "worker_cycle_started" for call in journal_calls)


def test_advance_workflow_registry_once_appends_stage_transition_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record = _registry_record(
        workflow_id="wf_stage_runtime",
        status="queued",
        workspace_dir="/tmp/wf_stage_runtime",
        stage_count=2,
    )
    _, journal_calls, _ = _capture_worker_side_effects(monkeypatch, records=[record])

    previous_summary = _summary_with_stages(
        {
            "stage_id": "crest_1",
            "status": "planned",
            "task_status": "planned",
            "engine": "crest",
            "task_kind": "conformer_search",
        },
        {
            "stage_id": "xtb_1",
            "status": "running",
            "task_status": "running",
            "engine": "xtb",
            "task_kind": "path_search",
        },
    )
    current_summary = _summary_with_stages(
        {
            "stage_id": "crest_1",
            "status": "queued",
            "task_status": "submitted",
            "engine": "crest",
            "task_kind": "conformer_search",
            "queue_id": "crest-q-1",
        },
        {
            "stage_id": "xtb_1",
            "status": "completed",
            "task_status": "completed",
            "engine": "xtb",
            "task_kind": "path_search",
            "reaction_handoff_status": "ready",
            "selected_input_xyz": "/tmp/ts_guess.xyz",
        },
    )
    summaries = iter([previous_summary, current_summary])

    monkeypatch.setattr(
        runtime,
        "_workflow_needs_terminal_sync",
        lambda workspace_dir: pytest.fail(
            "terminal sync checks should not run for active workflows"
        ),
    )
    monkeypatch.setattr(runtime, "_safe_workflow_summary", lambda *args, **kwargs: next(summaries))
    monkeypatch.setattr(
        runtime,
        "advance_workflow",
        lambda **kwargs: {
            "workflow_id": "wf_stage_runtime",
            "template_name": "reaction_ts_search",
            "status": "running",
            "stages": [{"stage_id": "crest_1"}, {"stage_id": "xtb_1"}],
        },
    )

    runtime.advance_workflow_registry_once(
        workflow_root=tmp_path / "workflow_root",
        worker_session_id="session-1",
        lease_seconds=0,
    )

    assert [call["event_type"] for call in journal_calls] == [
        "workflow_status_changed",
        "workflow_phase_finished",
        "workflow_stage_submitted",
        "workflow_stage_handoff_ready",
    ]
    assert journal_calls[1]["metadata"]["phase"] == "xtb"
    assert journal_calls[2]["metadata"]["stage_id"] == "crest_1"
    assert journal_calls[3]["status"] == "ready"
    assert journal_calls[3]["metadata"]["stage_id"] == "xtb_1"


def test_advance_workflow_registry_once_records_non_terminal_advance_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record = _registry_record(
        workflow_id="wf_failure",
        status="running",
        workspace_dir="/tmp/wf_failure",
        stage_count=4,
    )
    state_calls, journal_calls, _ = _capture_worker_side_effects(
        monkeypatch,
        records=[record],
    )

    monkeypatch.setattr(
        runtime,
        "_workflow_needs_terminal_sync",
        lambda workspace_dir: pytest.fail(
            "terminal sync checks should not run for active workflows"
        ),
    )

    def fake_advance_workflow(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime, "advance_workflow", fake_advance_workflow)

    result = runtime.advance_workflow_registry_once(
        workflow_root=tmp_path / "workflow_root",
        worker_session_id="session-1",
        lease_seconds=0,
    )

    assert result["advanced_count"] == 0
    assert result["skipped_count"] == 0
    assert result["failed_count"] == 1
    assert result["workflow_results"] == [
        {
            "workflow_id": "wf_failure",
            "template_name": "reaction_ts_search",
            "previous_status": "running",
            "status": "advance_failed",
            "advanced": False,
            "reason": "boom",
            "stage_count": 4,
        }
    ]
    assert state_calls == []
    assert [call["event_type"] for call in journal_calls] == ["workflow_advance_failed"]
    assert journal_calls[0]["reason"] == "boom"


def test_advance_workflow_registry_once_records_terminal_child_sync_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record = _registry_record(
        workflow_id="wf_terminal_failure",
        status="cancelled",
        workspace_dir="/tmp/wf_terminal_failure",
        stage_count=3,
    )
    _, journal_calls, _ = _capture_worker_side_effects(
        monkeypatch,
        records=[record],
    )

    monkeypatch.setattr(runtime, "_workflow_needs_terminal_sync", lambda workspace_dir: True)

    def fake_advance_workflow(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("sync broke")

    monkeypatch.setattr(runtime, "advance_workflow", fake_advance_workflow)

    result = runtime.advance_workflow_registry_once(
        workflow_root=tmp_path / "workflow_root",
        worker_session_id="session-1",
        lease_seconds=0,
    )

    assert result["advanced_count"] == 0
    assert result["skipped_count"] == 0
    assert result["failed_count"] == 1
    assert result["workflow_results"] == [
        {
            "workflow_id": "wf_terminal_failure",
            "template_name": "reaction_ts_search",
            "previous_status": "cancelled",
            "status": "advance_failed",
            "advanced": False,
            "reason": "terminal_child_sync_failed: sync broke",
            "stage_count": 3,
        }
    ]
    assert [call["event_type"] for call in journal_calls] == ["workflow_advance_failed"]
    assert journal_calls[0]["reason"] == "terminal_child_sync_failed: sync broke"
