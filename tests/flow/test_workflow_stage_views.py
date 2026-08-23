from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from orca_auto.flow.orchestration.stage_views import (
    WorkflowPayloadView,
    WorkflowStageView,
    WorkflowTaskView,
)


def test_task_view_mapping_fields_create_mutable_dicts() -> None:
    task: dict[str, Any] = {
        "payload": None,
        "metadata": "bad",
        "enqueue_payload": [],
        "submission_result": "",
    }
    view = WorkflowTaskView(task)

    view.update_payload({"selected_input_xyz": "/tmp/a.xyz"})
    view.update_metadata({"input_role": "reactant"})
    view.enqueue_payload()["priority"] = 7
    view.set_submission_result({"status": "submitted", "queue_id": "q1"})

    assert task["payload"] == {"selected_input_xyz": "/tmp/a.xyz"}
    assert task["metadata"] == {"input_role": "reactant"}
    assert task["enqueue_payload"] == {"priority": 7}
    assert task["submission_result"] == {"status": "submitted", "queue_id": "q1"}


def test_engine_contract_reason_is_persisted_in_stage_metadata() -> None:
    crest_stage: dict[str, Any] = {}
    xtb_stage: dict[str, Any] = {}

    WorkflowStageView(crest_stage).update_crest_contract_metadata(
        SimpleNamespace(
            job_id="crest-1",
            latest_known_path="/tmp/crest-1",
            reason="crest_exit_code_156",
        )
    )
    WorkflowStageView(xtb_stage).update_xtb_contract_metadata(
        SimpleNamespace(
            job_id="xtb-1",
            latest_known_path="/tmp/xtb-1",
            reason="xtb_ts_guess_missing",
        )
    )

    assert crest_stage["metadata"]["reason"] == "crest_exit_code_156"
    assert xtb_stage["metadata"]["reason"] == "xtb_ts_guess_missing"


def test_task_view_field_helpers_update_and_clear_nested_fields() -> None:
    task: dict[str, Any] = {
        "payload": {"stale": "x"},
        "enqueue_payload": {"priority": 1},
        "metadata": {"keep": True, "stale": "x"},
    }
    view = WorkflowTaskView(task)

    view.set_payload_field("selected_input_xyz", "/tmp/a.xyz")
    view.clear_payload_keys("stale", "missing")
    view.update_enqueue_payload({"priority": 9})
    view.set_metadata_field("attempt", 2)
    view.clear_metadata_keys("stale")

    assert task["payload"] == {"selected_input_xyz": "/tmp/a.xyz"}
    assert task["enqueue_payload"] == {"priority": 9}
    assert task["metadata"] == {"keep": True, "attempt": 2}


def test_task_view_existing_mapping_reads_do_not_coerce_bad_payloads() -> None:
    task: dict[str, Any] = {
        "enqueue_payload": "bad",
        "submission_result": {"status": "submitted"},
    }
    view = WorkflowTaskView(task)

    assert view.existing_mapping("enqueue_payload") is None
    assert view.existing_mapping("submission_result") == {"status": "submitted"}
    assert task["enqueue_payload"] == "bad"


def test_stage_view_status_pair_updates_stage_and_existing_task() -> None:
    stage: dict[str, Any] = {"status": "planned", "task": {"status": "planned"}}

    WorkflowStageView(stage).set_status_pair(stage_status="queued", task_status="submitted")

    assert stage["status"] == "queued"
    assert stage["task"]["status"] == "submitted"


def test_stage_view_status_pair_ignores_missing_task() -> None:
    stage = {"status": "planned", "task": None}

    WorkflowStageView(stage).set_status_pair(stage_status="cancelled", task_status="cancelled")

    assert stage["status"] == "cancelled"
    assert stage["task"] is None


def test_stage_view_status_pair_snapshot_uses_normalizer() -> None:
    stage: dict[str, Any] = {
        "status": " Queued ",
        "task": {"status": " Submitted "},
    }

    status = WorkflowStageView(stage).status_pair_with(lambda value: str(value).strip())

    assert status.stage == "queued"
    assert status.task == "submitted"
    assert status.any_status("submitted") is True
    assert status.any_matches(lambda value: value.endswith("ed")) is True


def test_stage_view_ensure_task_creates_mutable_task_mapping() -> None:
    stage: dict[str, Any] = {"task": None}

    task = WorkflowStageView(stage).ensure_task()
    task.set_status("planned")

    assert stage["task"] == {"status": "planned"}


def test_stage_view_output_artifacts_replaces_existing_value() -> None:
    stage = {"output_artifacts": "bad"}
    artifacts = [{"kind": "orca_last_out", "path": "/tmp/out"}]

    WorkflowStageView(stage).set_output_artifacts(artifacts)

    assert stage["output_artifacts"] == artifacts


def test_stage_view_metadata_helpers_update_and_clear_fields() -> None:
    stage = {"metadata": {"keep": True, "stale": "x"}}
    view = WorkflowStageView(stage)

    view.set_metadata_field("queue_id", "q1")
    view.clear_metadata_keys("stale", "missing")

    assert stage["metadata"] == {"keep": True, "queue_id": "q1"}


def test_stage_view_xtb_attempt_helpers_filter_sort_and_find_rows() -> None:
    stage: dict[str, Any] = {
        "metadata": {
            "xtb_attempts": [
                {"attempt_number": "2", "status": "failed"},
                "skip",
                {"attempt_number": 0, "status": "completed"},
            ]
        }
    }
    view = WorkflowStageView(stage)

    attempt = view.xtb_attempt_record(1)

    assert attempt == {"attempt_number": 1}
    assert stage["metadata"]["xtb_attempts"] == [
        {"attempt_number": 0, "status": "completed"},
        {"attempt_number": 1},
        {"attempt_number": "2", "status": "failed"},
    ]
    assert view.xtb_current_attempt_number() == 2


def test_stage_view_reaction_handoff_sets_and_clears_optional_fields() -> None:
    stage: dict[str, Any] = {
        "metadata": {
            "reaction_handoff_reason": "old",
            "reaction_handoff_message": "old",
            "reaction_handoff_artifact_path": "/old",
        }
    }
    view = WorkflowStageView(stage)

    view.set_reaction_handoff(
        {
            "status": "ready",
            "reason": "",
            "message": "",
            "artifact_path": "/tmp/ts_guess.xyz",
        }
    )

    assert stage["metadata"] == {
        "reaction_handoff_status": "ready",
        "reaction_handoff_artifact_path": "/tmp/ts_guess.xyz",
    }


def test_payload_view_filters_stage_views_and_preserves_bad_metadata() -> None:
    payload: dict[str, Any] = {
        "workflow_id": " wf_01 ",
        "status": " Running ",
        "metadata": "bad",
        "stages": ["skip", {"stage_id": "stage_1"}],
    }
    view = WorkflowPayloadView(payload)

    view.set_status("queued")

    assert [stage.raw for stage in view.stage_views] == [{"stage_id": "stage_1"}]
    assert view.workflow_id(lambda value: str(value).strip()) == "wf_01"
    assert view.status(lambda value: str(value).strip()) == "queued"
    assert view.metadata() is None
    assert payload["metadata"] == "bad"
