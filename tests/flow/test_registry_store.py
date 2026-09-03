from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orca_auto.flow import registry
from orca_auto.flow.registry import store as registry_store
from orca_auto.flow.workflow.store import WORKFLOW_CREATION_MARKER_FILE
from tests.flow.registry_test_helpers import (
    patch_file_locks as _patch_file_locks,
)
from tests.flow.registry_test_helpers import (
    patch_now_utc_iso as _patch_now_utc_iso,
)


def test_record_from_summary_coerces_counts_and_nested_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_now_utc_iso(monkeypatch, lambda: "2026-04-19T00:00:00+00:00")

    record = registry_store._record_from_summary(
        {
            "workflow_id": "wf_1",
            "template_name": "reaction_ts_search",
            "status": "planned",
            "source_job_id": "job_1",
            "source_job_type": "xtb_path",
            "reaction_key": "rxn_1",
            "requested_at": "2026-04-19T00:00:00+00:00",
            "workspace_dir": "/tmp/workspace_1",
            "stage_count": "2",
            "stage_status_counts": {"planned": "2", "bad": "nan"},
            "task_status_counts": {"submitted": 1},
            "downstream_reaction_workflow": {"workflow_id": "child_1"},
            "precomplex_handoff": {"reactant_xyz": "/tmp/reactant.xyz"},
            "parent_workflow": {"workflow_id": "parent_1"},
            "final_child_sync_pending": 1,
            "last_restarted_at": "2026-04-19T00:45:00+00:00",
            "restart_summary": {"status": "restarted", "restarted_at": "2026-04-19T00:45:00+00:00"},
        }
    )

    assert record.workflow_id == "wf_1"
    assert record.workflow_file == str(Path("/tmp/workspace_1").resolve() / "workflow.json")
    assert record.stage_count == 2
    assert record.updated_at  # stamped at record-build time
    assert record.stage_status_counts == {"planned": 2}
    assert record.task_status_counts == {"submitted": 1}
    assert record.metadata == {
        "downstream_reaction_workflow": {"workflow_id": "child_1"},
        "precomplex_handoff": {"reactant_xyz": "/tmp/reactant.xyz"},
        "parent_workflow": {"workflow_id": "parent_1"},
        "final_child_sync_pending": True,
        "last_restarted_at": "2026-04-19T00:45:00+00:00",
        "restart_summary": {"status": "restarted", "restarted_at": "2026-04-19T00:45:00+00:00"},
    }


def test_clear_terminal_workflow_registry_removes_only_terminal_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)

    records = [
        registry.WorkflowRegistryRecord(
            workflow_id="wf-completed",
            template_name="reaction_ts_search",
            status="completed",
            source_job_id="job-1",
            source_job_type="reaction_ts_search",
            reaction_key="rxn-1",
            requested_at="2026-04-19T00:00:00+00:00",
            workspace_dir=str(tmp_path / "wf-completed"),
            workflow_file=str(tmp_path / "wf-completed" / "workflow.json"),
        ),
        registry.WorkflowRegistryRecord(
            workflow_id="wf-running",
            template_name="reaction_ts_search",
            status="running",
            source_job_id="job-2",
            source_job_type="reaction_ts_search",
            reaction_key="rxn-2",
            requested_at="2026-04-19T00:01:00+00:00",
            workspace_dir=str(tmp_path / "wf-running"),
            workflow_file=str(tmp_path / "wf-running" / "workflow.json"),
        ),
        registry.WorkflowRegistryRecord(
            workflow_id="wf-cancelled",
            template_name="reaction_ts_search",
            status="cancelled",
            source_job_id="job-3",
            source_job_type="reaction_ts_search",
            reaction_key="rxn-3",
            requested_at="2026-04-19T00:02:00+00:00",
            workspace_dir=str(tmp_path / "wf-cancelled"),
            workflow_file=str(tmp_path / "wf-cancelled" / "workflow.json"),
        ),
    ]
    registry_store._save_records(tmp_path, records)

    assert registry.clear_terminal_workflow_registry(tmp_path) == 2
    remaining = registry.list_workflow_registry(tmp_path, reindex_if_missing=False)
    assert [record.workflow_id for record in remaining] == ["wf-running"]
    assert registry.clear_terminal_workflow_registry(tmp_path) == 0


def test_clear_terminal_workflow_registry_prevents_reindex_resurrection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    _patch_now_utc_iso(monkeypatch, lambda: "2026-04-19T00:20:00+00:00")

    completed_workspace = tmp_path / "wf-completed"
    running_workspace = tmp_path / "wf-running"
    completed_workspace.mkdir()
    running_workspace.mkdir()
    completed_payload = {
        "workflow_id": "wf-completed",
        "template_name": "reaction_ts_search",
        "status": "completed",
        "source_job_id": "job-1",
        "source_job_type": "reaction_ts_search",
        "reaction_key": "rxn-1",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {},
    }
    (completed_workspace / "workflow.json").write_text(
        json.dumps(completed_payload), encoding="utf-8"
    )
    (running_workspace / "workflow.json").write_text(
        json.dumps(
            {
                "workflow_id": "wf-running",
                "template_name": "reaction_ts_search",
                "status": "running",
                "source_job_id": "job-2",
                "source_job_type": "reaction_ts_search",
                "reaction_key": "rxn-2",
                "requested_at": "2026-04-19T00:01:00+00:00",
                "stages": [],
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )

    assert registry.clear_terminal_workflow_registry(tmp_path) == 1
    assert [
        record.workflow_id
        for record in registry.list_workflow_registry(tmp_path, reindex_if_missing=False)
    ] == ["wf-running"]

    reindexed = registry.reindex_workflow_registry(tmp_path)
    assert [record.workflow_id for record in reindexed] == ["wf-running"]

    completed_payload["status"] = "running"
    (completed_workspace / "workflow.json").write_text(
        json.dumps(completed_payload), encoding="utf-8"
    )
    assert {record.workflow_id for record in registry.reindex_workflow_registry(tmp_path)} == {
        "wf-completed",
        "wf-running",
    }

    completed_payload["status"] = "completed"
    (completed_workspace / "workflow.json").write_text(
        json.dumps(completed_payload), encoding="utf-8"
    )
    assert {record.workflow_id for record in registry.reindex_workflow_registry(tmp_path)} == {
        "wf-completed",
        "wf-running",
    }


def test_terminal_workflow_with_pending_si_publication_is_not_clearable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf-pending-si"
    workspace.mkdir()
    payload = {
        "workflow_id": "wf-pending-si",
        "template_name": "conformer_screening",
        "status": "completed",
        "source_job_id": "job-pending-si",
        "source_job_type": "conformer_screening",
        "reaction_key": "input",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {"si_publish_pending": True, "si_publish_generation": "generation-1"},
    }
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")
    registry.sync_workflow_registry(tmp_path, workspace, payload)

    assert registry.clear_terminal_workflow_registry(tmp_path) == 0
    records = registry.list_workflow_registry(tmp_path, reindex_if_missing=False)
    assert [
        (record.workflow_id, record.metadata.get("si_publish_pending")) for record in records
    ] == [("wf-pending-si", True)]
    assert not registry_store._cleared_path(tmp_path).exists()

    reindexed = registry.reindex_workflow_registry(tmp_path)
    assert [
        (record.workflow_id, record.metadata.get("si_publish_pending")) for record in reindexed
    ] == [("wf-pending-si", True)]


@pytest.mark.parametrize("pending_flag", ["si_publish_pending", "final_child_sync_pending"])
def test_clear_consults_authoritative_pending_when_registry_sync_lagged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pending_flag: str,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf-authoritative-pending"
    workspace.mkdir()
    payload = {
        "workflow_id": "wf-authoritative-pending",
        "template_name": "conformer_screening",
        "status": "completed",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {pending_flag: True},
    }
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")
    stale_record = registry.WorkflowRegistryRecord(
        workflow_id="wf-authoritative-pending",
        template_name="conformer_screening",
        status="completed",
        source_job_id="",
        source_job_type="",
        reaction_key="",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(workspace),
        workflow_file=str(workspace / "workflow.json"),
    )
    registry_store._save_records(tmp_path, [stale_record])
    assert registry.clear_terminal_workflow_registry(tmp_path) == 0
    assert registry.get_workflow_registry_record(tmp_path, "wf-authoritative-pending") is not None


def test_clear_keeps_stale_terminal_row_when_authoritative_restart_is_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf-restart-sync-lag"
    workspace.mkdir()
    active_payload = {
        "workflow_id": "wf-restart-sync-lag",
        "template_name": "conformer_screening",
        "status": "planned",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {},
    }
    (workspace / "workflow.json").write_text(json.dumps(active_payload), encoding="utf-8")
    stale_record = registry.WorkflowRegistryRecord(
        workflow_id="wf-restart-sync-lag",
        template_name="conformer_screening",
        status="failed",
        source_job_id="",
        source_job_type="",
        reaction_key="",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(workspace),
        workflow_file=str(workspace / "workflow.json"),
    )
    registry_store._save_records(tmp_path, [stale_record])

    assert registry.clear_terminal_workflow_registry(tmp_path) == 0
    assert registry.get_workflow_registry_record(tmp_path, "wf-restart-sync-lag") is not None


def test_clear_keeps_row_while_pending_checkpoint_holds_workflow_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf-checkpoint-race"
    workspace.mkdir()
    payload: dict[str, Any] = {
        "workflow_id": "wf-checkpoint-race",
        "template_name": "conformer_screening",
        "status": "completed",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {},
    }
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")
    registry.sync_workflow_registry(tmp_path, workspace, payload)

    with registry_store.acquire_workflow_lock(workspace):
        payload["metadata"]["si_publish_pending"] = True
        (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")
        # Simulate a crash/pause before registry sync. Clear must fail closed
        # instead of observing the old cached row and racing the checkpoint.
        assert registry.clear_terminal_workflow_registry(tmp_path) == 0

    record = registry.get_workflow_registry_record(tmp_path, "wf-checkpoint-race")
    assert record is not None


def test_clear_never_locks_registry_workspace_outside_direct_root_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "nested" / "wf-outside"
    workspace.mkdir(parents=True)
    (workspace / "workflow.json").write_text(
        json.dumps(
            {
                "workflow_id": "wf-outside",
                "template_name": "conformer_screening",
                "status": "completed",
                "stages": [],
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    record = registry.WorkflowRegistryRecord(
        workflow_id="wf-outside",
        template_name="conformer_screening",
        status="completed",
        source_job_id="",
        source_job_type="",
        reaction_key="",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(workspace),
        workflow_file=str(workspace / "workflow.json"),
    )
    registry_store._save_records(tmp_path, [record])

    assert registry.clear_terminal_workflow_registry(tmp_path) == 0
    assert not (workspace / "workflow.lock").exists()
    assert registry.get_workflow_registry_record(tmp_path, "wf-outside") is not None


def test_clear_missing_payload_does_not_create_workflow_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf-missing-payload"
    workspace.mkdir()
    record = registry.WorkflowRegistryRecord(
        workflow_id="wf-missing-payload",
        template_name="conformer_screening",
        status="completed",
        source_job_id="",
        source_job_type="",
        reaction_key="",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(workspace),
        workflow_file=str(workspace / "workflow.json"),
    )
    registry_store._save_records(tmp_path, [record])

    assert registry.clear_terminal_workflow_registry(tmp_path) == 1
    assert not (workspace / "workflow.lock").exists()


@pytest.mark.parametrize(
    "flag", ["si_publish_pending", "si_publish_blocked", "final_child_sync_pending"]
)
def test_stale_registry_publication_guard_is_not_clearable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag: str,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / f"wf-{flag}"
    workspace.mkdir()
    (workspace / "workflow.json").write_text(
        json.dumps(
            {
                "workflow_id": f"wf-{flag}",
                "template_name": "conformer_screening",
                "status": "completed",
                "stages": [],
                "metadata": {flag: False},
            }
        ),
        encoding="utf-8",
    )
    record = registry.WorkflowRegistryRecord(
        workflow_id=f"wf-{flag}",
        template_name="conformer_screening",
        status="completed",
        source_job_id="",
        source_job_type="",
        reaction_key="",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(workspace),
        workflow_file=str(workspace / "workflow.json"),
        metadata={flag: True},
    )
    registry_store._save_records(tmp_path, [record])
    assert registry.clear_terminal_workflow_registry(tmp_path) == 0


def test_sync_quarantined_identity_uses_workspace_key_without_mutating_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf_expected"
    workspace.mkdir()
    payload = {
        "workflow_id": "wf_tampered",
        "template_name": "conformer_screening",
        "status": "failed",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {
            "workflow_error": {
                "status": "failed",
                "scope": "workflow_identity_validation",
            }
        },
    }

    record = registry.sync_workflow_registry(tmp_path, workspace, payload)

    assert payload["workflow_id"] == "wf_tampered"
    assert record.workflow_id == "wf_expected"
    assert record.metadata["quarantined_persisted_workflow_id"] == "wf_tampered"


def test_sync_prequarantine_identity_mismatch_stays_visible_by_workspace_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf_expected"
    workspace.mkdir()
    payload = {
        "workflow_id": "wf_tampered",
        "template_name": "conformer_screening",
        "status": "completed",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {},
    }

    record = registry.sync_workflow_registry(tmp_path, workspace, payload)

    # The durable payload is left alone and the row stays addressable by its
    # trusted workspace identity. A row that has not been quarantined yet
    # carries no cached identity marker; the authoritative recheck happens
    # under the lock at clear time.
    assert payload["workflow_id"] == "wf_tampered"
    assert record.workflow_id == "wf_expected"
    assert "identity_reconciliation_persisted_workflow_id" not in record.metadata
    assert "identity_quarantined" not in record.metadata


def test_identity_quarantine_resurrects_a_previously_cleared_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf_expected"
    workspace.mkdir()
    valid_payload = {
        "workflow_id": "wf_expected",
        "template_name": "conformer_screening",
        "status": "completed",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {},
    }
    (workspace / "workflow.json").write_text(json.dumps(valid_payload), encoding="utf-8")
    registry.sync_workflow_registry(tmp_path, workspace, valid_payload)
    assert registry.clear_terminal_workflow_registry(tmp_path) == 1

    quarantined_payload = {
        **valid_payload,
        "workflow_id": "wf_tampered",
        "status": "failed",
        "metadata": {
            "workflow_error": {
                "status": "failed",
                "scope": "workflow_identity_validation",
            }
        },
    }
    (workspace / "workflow.json").write_text(json.dumps(quarantined_payload), encoding="utf-8")

    registry.sync_workflow_registry(tmp_path, workspace, quarantined_payload)

    records = registry.list_workflow_registry(tmp_path, reindex_if_missing=False)
    assert [(record.workflow_id, record.status) for record in records] == [
        ("wf_expected", "failed")
    ]
    assert records[0].metadata["quarantined_persisted_workflow_id"] == "wf_tampered"
    assert registry_store._load_cleared_markers(tmp_path) == []


def test_upsert_replaces_all_rows_for_the_same_trusted_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf_expected"
    workspace.mkdir()

    def record(workflow_id: str, status: str) -> registry.WorkflowRegistryRecord:
        return registry.WorkflowRegistryRecord(
            workflow_id=workflow_id,
            template_name="conformer_screening",
            status=status,
            source_job_id="",
            source_job_type="",
            reaction_key="",
            requested_at="2026-04-19T00:00:00+00:00",
            workspace_dir=str(workspace),
            workflow_file=str(workspace / "workflow.json"),
        )

    registry_store._save_records(
        tmp_path,
        [record("wf_expected", "running"), record("wf_tampered", "failed")],
    )
    replacement = record("wf_expected", "failed")

    registry.upsert_workflow_registry_record(tmp_path, replacement)

    assert registry.list_workflow_registry(tmp_path, reindex_if_missing=False) == [replacement]


def test_upsert_rejects_unvalidated_direct_workspace_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    workspace = tmp_path / "wf_expected"
    workspace.mkdir()
    expected = registry.WorkflowRegistryRecord(
        workflow_id="wf_expected",
        template_name="conformer_screening",
        status="running",
        source_job_id="",
        source_job_type="",
        reaction_key="",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(workspace),
        workflow_file=str(workspace / "workflow.json"),
    )
    registry_store._save_records(tmp_path, [expected])
    tampered = replace(expected, workflow_id="wf_tampered", status="cancelled")

    with pytest.raises(ValueError, match="does not match workspace name"):
        registry.upsert_workflow_registry_record(tmp_path, tampered)

    assert registry.list_workflow_registry(tmp_path, reindex_if_missing=False) == [expected]


def test_sync_skips_cleared_terminal_workflow_until_it_becomes_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    _patch_now_utc_iso(monkeypatch, lambda: "2026-04-19T00:20:00+00:00")

    workspace = tmp_path / "wf-completed"
    workspace.mkdir()
    terminal_payload = {
        "workflow_id": "wf-completed",
        "template_name": "reaction_ts_search",
        "status": "completed",
        "source_job_id": "job-1",
        "source_job_type": "reaction_ts_search",
        "reaction_key": "rxn-1",
        "requested_at": "2026-04-19T00:00:00+00:00",
        "stages": [],
        "metadata": {},
    }
    (workspace / "workflow.json").write_text(json.dumps(terminal_payload), encoding="utf-8")

    assert registry.clear_terminal_workflow_registry(tmp_path) == 1
    registry.sync_workflow_registry(tmp_path, workspace, terminal_payload)
    assert registry.list_workflow_registry(tmp_path, reindex_if_missing=False) == []

    active_payload = dict(terminal_payload)
    active_payload["status"] = "running"
    registry.sync_workflow_registry(tmp_path, workspace, active_payload)
    records = registry.list_workflow_registry(tmp_path, reindex_if_missing=False)
    assert [(record.workflow_id, record.status) for record in records] == [
        ("wf-completed", "running")
    ]


@pytest.mark.parametrize("markers_payload", ["{invalid", json.dumps({"bad": True})])
def test_workflow_cleared_markers_corrupt_payload_blocks_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    markers_payload: str,
) -> None:
    _patch_file_locks(monkeypatch)
    record = registry.WorkflowRegistryRecord(
        workflow_id="wf_completed",
        template_name="reaction_ts_search",
        status="completed",
        source_job_id="job_completed",
        source_job_type="xtb_path",
        reaction_key="rxn_completed",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(tmp_path / "wf_completed"),
        workflow_file=str(tmp_path / "wf_completed" / "workflow.json"),
    )
    registry_store._save_records(tmp_path, [record])
    registry_text = registry_store._registry_path(tmp_path).read_text(encoding="utf-8")
    registry_store._cleared_path(tmp_path).write_text(markers_payload, encoding="utf-8")

    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.clear_terminal_workflow_registry(tmp_path)
    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.upsert_workflow_registry_record(tmp_path, record)
    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.reindex_workflow_registry(tmp_path)
    assert registry_store._registry_path(tmp_path).read_text(encoding="utf-8") == registry_text
    assert registry_store._cleared_path(tmp_path).read_text(encoding="utf-8") == markers_payload


def test_list_workflow_registry_does_not_reindex_valid_empty_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    registry_store._save_records(tmp_path, [])

    def fake_reindex_workflow_registry(root: str | Path) -> list[registry.WorkflowRegistryRecord]:
        raise AssertionError(f"unexpected reindex for {root}")

    monkeypatch.setattr(registry_store, "reindex_workflow_registry", fake_reindex_workflow_registry)

    assert registry.list_workflow_registry(tmp_path) == []


def test_list_workflow_registry_repairs_published_creation_marker_before_cached_return(
    tmp_path: Path,
) -> None:
    registry_store._save_records(tmp_path, [])
    workspace = tmp_path / "wf_crash_repair"
    workspace.mkdir()
    (workspace / "workflow.json").write_text("{}", encoding="utf-8")
    marker = workspace / WORKFLOW_CREATION_MARKER_FILE
    marker.write_text("{}", encoding="utf-8")
    record = registry.WorkflowRegistryRecord(
        workflow_id=workspace.name,
        template_name="conformer_screening",
        status="planned",
        source_job_id="",
        source_job_type="raw_xyz",
        reaction_key="mol",
        requested_at="2026-07-13T00:00:00+00:00",
        workspace_dir=str(workspace.resolve()),
        workflow_file=str((workspace / "workflow.json").resolve()),
    )
    calls: list[Path] = []

    def repair(root: str | Path) -> list[registry.WorkflowRegistryRecord]:
        calls.append(Path(root).resolve())
        return [record]

    assert registry.list_workflow_registry(tmp_path, reindex_fn=repair) == [record]
    assert calls == [tmp_path.resolve()]
    assert not marker.exists()


def test_list_workflow_registry_keeps_marker_when_reindex_skips_workspace(tmp_path: Path) -> None:
    registry_store._save_records(tmp_path, [])
    workspace = tmp_path / "wf_corrupt_repair"
    workspace.mkdir()
    (workspace / "workflow.json").write_text("not-json", encoding="utf-8")
    marker = workspace / WORKFLOW_CREATION_MARKER_FILE
    marker.write_text("{}", encoding="utf-8")

    assert registry.list_workflow_registry(tmp_path, reindex_fn=lambda _root: []) == []
    assert marker.exists()


def test_list_workflow_registry_ignores_unpublished_creation_marker(tmp_path: Path) -> None:
    registry_store._save_records(tmp_path, [])
    workspace = tmp_path / "wf_incomplete_reservation"
    workspace.mkdir()
    marker = workspace / WORKFLOW_CREATION_MARKER_FILE
    marker.write_text("{}", encoding="utf-8")

    def unexpected_reindex(_root: str | Path) -> list[registry.WorkflowRegistryRecord]:
        raise AssertionError("unpublished reservations must not trigger reindex")

    assert registry.list_workflow_registry(tmp_path, reindex_fn=unexpected_reindex) == []
    assert marker.exists()


def test_list_workflow_registry_missing_without_reindex_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)

    assert registry.list_workflow_registry(tmp_path, reindex_if_missing=False) == []
    assert not registry_store._registry_path(tmp_path).exists()


def test_clear_terminal_workflow_registry_empty_status_filter_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    record = registry.WorkflowRegistryRecord(
        workflow_id="wf-completed",
        template_name="reaction_ts_search",
        status="completed",
        source_job_id="job-1",
        source_job_type="xtb_path",
        reaction_key="rxn-1",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(tmp_path / "wf-completed"),
        workflow_file=str(tmp_path / "wf-completed" / "workflow.json"),
    )
    registry_store._save_records(tmp_path, [record])

    assert registry.clear_terminal_workflow_registry(tmp_path, statuses={"", "  "}) == 0
    assert registry.list_workflow_registry(tmp_path, reindex_if_missing=False) == [record]
    assert not registry_store._cleared_path(tmp_path).exists()


def test_registry_read_os_error_raises_corrupt_error() -> None:
    class BrokenPath:
        def exists(self) -> bool:
            return True

        def read_text(self, *, encoding: str) -> str:
            raise OSError("permission denied")

        def __str__(self) -> str:
            return "/broken/workflow_registry.json"

    with pytest.raises(registry.WorkflowRegistryCorruptError, match="cannot be read"):
        registry_store._read_existing_json(
            BrokenPath(),  # type: ignore[arg-type]
            description="Workflow registry file",
            missing_default=[],
        )


@pytest.mark.parametrize("payload", ["{invalid", json.dumps({"workflow_id": "wf-bad"})])
def test_list_workflow_registry_rejects_corrupt_existing_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: str,
) -> None:
    _patch_file_locks(monkeypatch)
    registry_store._registry_path(tmp_path).write_text(payload, encoding="utf-8")
    reindex_calls: list[Path] = []

    def fake_reindex_workflow_registry(root: str | Path) -> list[registry.WorkflowRegistryRecord]:
        reindex_calls.append(Path(root).resolve())
        return []

    monkeypatch.setattr(registry_store, "reindex_workflow_registry", fake_reindex_workflow_registry)

    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.list_workflow_registry(tmp_path)
    assert reindex_calls == []
    assert registry_store._registry_path(tmp_path).read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("registry_payload", ["{invalid", json.dumps({"workflow_id": "wf-bad"})])
def test_workflow_registry_writes_reject_corrupt_existing_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    registry_payload: str,
) -> None:
    _patch_file_locks(monkeypatch)
    registry_store._registry_path(tmp_path).write_text(registry_payload, encoding="utf-8")
    record = registry.WorkflowRegistryRecord(
        workflow_id="wf_safe",
        template_name="reaction_ts_search",
        status="planned",
        source_job_id="job_safe",
        source_job_type="xtb_path",
        reaction_key="rxn_safe",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(tmp_path / "wf_safe"),
        workflow_file=str(tmp_path / "wf_safe" / "workflow.json"),
    )

    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.upsert_workflow_registry_record(tmp_path, record)
    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.reindex_workflow_registry(tmp_path)
    assert registry_store._registry_path(tmp_path).read_text(encoding="utf-8") == registry_payload


def test_upsert_list_get_and_resolve_workflow_registry_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    record_older = registry.WorkflowRegistryRecord(
        workflow_id="wf_a",
        template_name="reaction_ts_search",
        status="planned",
        source_job_id="job_a",
        source_job_type="xtb_path",
        reaction_key="rxn_a",
        requested_at="2026-04-19T00:00:00+00:00",
        workspace_dir=str(tmp_path / "wf_a"),
        workflow_file=str(tmp_path / "wf_a" / "workflow.json"),
    )
    record_newer = registry.WorkflowRegistryRecord(
        workflow_id="wf_b",
        template_name="conformer_screening",
        status="running",
        source_job_id="job_b",
        source_job_type="raw_xyz",
        reaction_key="rxn_b",
        requested_at="2026-04-19T00:05:00+00:00",
        workspace_dir=str(tmp_path / "wf_b"),
        workflow_file=str(tmp_path / "wf_b" / "workflow.json"),
    )
    record_updated = registry.WorkflowRegistryRecord(
        workflow_id="wf_a",
        template_name="reaction_ts_search",
        status="completed",
        source_job_id="job_a",
        source_job_type="xtb_path",
        reaction_key="rxn_a",
        requested_at="2026-04-19T00:10:00+00:00",
        workspace_dir=str(tmp_path / "wf_a"),
        workflow_file=str(tmp_path / "wf_a" / "workflow.json"),
    )

    registry.upsert_workflow_registry_record(tmp_path, record_older)
    registry.upsert_workflow_registry_record(tmp_path, record_newer)
    registry.upsert_workflow_registry_record(tmp_path, record_updated)

    records = registry.list_workflow_registry(tmp_path, reindex_if_missing=False)

    assert [(item.workflow_id, item.status) for item in records] == [
        ("wf_a", "completed"),
        ("wf_b", "running"),
    ]
    assert registry.get_workflow_registry_record(tmp_path, "wf_b") == record_newer
    assert registry.resolve_workflow_registry_record(tmp_path, "wf_a") == record_updated
    assert (
        registry.resolve_workflow_registry_record(tmp_path, str(tmp_path / "wf_b")) == record_newer
    )
    assert (
        registry.resolve_workflow_registry_record(
            tmp_path, str(tmp_path / "wf_a" / "workflow.json")
        )
        == record_updated
    )
    assert registry.resolve_workflow_registry_record(tmp_path, "") is None


def test_list_workflow_registry_reindexes_when_missing_and_reindex_skips_bad_workspaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    original_reindex = registry.reindex_workflow_registry

    list_result = [
        registry.WorkflowRegistryRecord(
            workflow_id="wf_reindexed",
            template_name="reaction_ts_search",
            status="planned",
            source_job_id="job_reindexed",
            source_job_type="xtb_path",
            reaction_key="rxn_reindexed",
            requested_at="2026-04-19T00:00:00+00:00",
            workspace_dir=str(tmp_path / "wf_reindexed"),
            workflow_file=str(tmp_path / "wf_reindexed" / "workflow.json"),
        )
    ]
    reindex_calls: list[Path] = []

    def fake_reindex_workflow_registry(root: str | Path) -> list[registry.WorkflowRegistryRecord]:
        reindex_calls.append(Path(root).resolve())
        return list_result

    monkeypatch.setattr(registry_store, "reindex_workflow_registry", fake_reindex_workflow_registry)
    assert registry.list_workflow_registry(tmp_path) == list_result
    assert reindex_calls == [tmp_path.resolve()]
    monkeypatch.setattr(registry_store, "reindex_workflow_registry", original_reindex)

    good_workspace = tmp_path / "wf_good"
    bad_workspace = tmp_path / "wf_bad"
    summaries = {
        good_workspace: {
            "workflow_id": "wf_good",
            "template_name": "conformer_screening",
            "status": "planned",
            "source_job_id": "job_good",
            "source_job_type": "raw_xyz",
            "reaction_key": "rxn_good",
            "requested_at": "2026-04-19T00:15:00+00:00",
            "workspace_dir": str(good_workspace),
            "stage_count": 1,
            "stage_status_counts": {"planned": 1},
            "task_status_counts": {"planned": 1},
        }
    }

    def fake_iter_workflow_workspaces(root: Path) -> list[Path]:
        return [good_workspace, bad_workspace]

    def fake_load_workflow_payload(workspace_dir: Path) -> dict[str, Any]:
        if workspace_dir == bad_workspace:
            raise FileNotFoundError("missing workflow")
        return {"workflow_id": "wf_good"}

    def fake_workflow_summary(workspace_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
        return summaries[workspace_dir]

    monkeypatch.setattr(registry_store, "iter_workflow_workspaces", fake_iter_workflow_workspaces)
    monkeypatch.setattr(registry_store, "load_workflow_payload", fake_load_workflow_payload)
    monkeypatch.setattr(registry_store, "workflow_summary", fake_workflow_summary)
    _patch_now_utc_iso(monkeypatch, lambda: "2026-04-19T00:20:00+00:00")

    records = registry.reindex_workflow_registry(tmp_path)

    assert len(records) == 1
    assert records[0].workflow_id == "wf_good"
    assert (
        registry.sync_workflow_registry(
            tmp_path, good_workspace, {"workflow_id": "wf_good"}
        ).workflow_id
        == "wf_good"
    )


def test_clear_terminal_keeps_a_record_with_undrained_cancel_transitions(tmp_path: Path) -> None:
    # A cancel command that crashed before journaling leaves transitions on
    # the payload; the worker drains them from this registry row, so the row
    # must survive a terminal clear until then.
    from orca_auto.flow.state import write_workflow_payload

    workspace = tmp_path / "wf-cancelled"
    workspace.mkdir()
    transition = {
        "event_id": "wf_evt_1",
        "occurred_at": "2026-08-11T05:20:00+00:00",
        "previous_status": "running",
        "status": "cancelled",
    }
    payload = {
        "workflow_id": "wf-cancelled",
        "template_name": "reaction_ts_search",
        "status": "cancelled",
        "stages": [],
        "metadata": {"cancellation_status_transitions": [transition]},
    }
    write_workflow_payload(workspace, payload)
    registry_store._save_records(
        tmp_path,
        [
            registry.WorkflowRegistryRecord(
                workflow_id="wf-cancelled",
                template_name="reaction_ts_search",
                status="cancelled",
                source_job_id="job-3",
                source_job_type="reaction_ts_search",
                reaction_key="rxn-3",
                requested_at="2026-04-19T00:02:00+00:00",
                workspace_dir=str(workspace),
                workflow_file=str(workspace / "workflow.json"),
            )
        ],
    )

    assert registry.clear_terminal_workflow_registry(tmp_path) == 0

    payload["metadata"] = {"cancellation_status_transitions": []}
    write_workflow_payload(workspace, payload)
    assert registry.clear_terminal_workflow_registry(tmp_path) == 1
