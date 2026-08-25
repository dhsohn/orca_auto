from __future__ import annotations

import pytest

from orca_auto.flow.contracts import (
    WorkflowArtifactRef,
    WorkflowPlan,
    WorkflowStage,
    WorkflowTask,
    WorkflowTemplateRequest,
)
from orca_auto.flow.contracts.workflow import (
    coerce_workflow_plan_payload,
    required_route_line,
    workflow_metadata,
    workflow_request_parameters,
)


def test_workflow_plan_to_dict_preserves_nested_stage_task_and_artifact_payloads() -> None:
    task = WorkflowTask.from_raw(
        task_id="wf:crest_reactant",
        engine="crest",
        task_kind="conformer_search",
        resource_request={"max_cores": "4", "max_memory_gb": 16},
        payload={"mode": "nci"},
        enqueue_payload={"priority": 7},
        metadata={"input_role": "reactant"},
    )
    stage = WorkflowStage(
        stage_id="crest_reactant",
        stage_kind="crest_stage",
        status="planned",
        input_artifacts=(
            WorkflowArtifactRef(
                kind="input_xyz",
                path="/tmp/reactant.xyz",
                selected=True,
                metadata={"input_role": "reactant"},
            ),
        ),
        output_artifacts=(WorkflowArtifactRef(kind="crest_conformers", path="/tmp/crest.xyz"),),
        task=task,
        metadata={"input_role": "reactant"},
    )
    plan = WorkflowPlan(
        workflow_id="wf_1",
        template_name="reaction_ts_search",
        status="planned",
        source_job_id="src_1",
        source_job_type="manual",
        reaction_key="rxn_1",
        requested_at="2026-05-20T00:00:00+00:00",
        stages=(stage,),
        metadata={"workspace_dir": "/tmp/wf_1"},
    )

    payload = plan.to_dict()

    assert payload["workflow_id"] == "wf_1"
    assert payload["metadata"] == {"workspace_dir": "/tmp/wf_1"}
    stage_payload = payload["stages"][0]
    assert stage_payload["input_artifacts"][0] == {
        "kind": "input_xyz",
        "path": "/tmp/reactant.xyz",
        "selected": True,
        "metadata": {"input_role": "reactant"},
    }
    assert stage_payload["output_artifacts"][0]["metadata"] == {}
    task_payload = stage_payload["task"]
    assert task_payload is not None
    assert task_payload["resource_request"] == {"max_cores": 4, "max_memory_gb": 16}
    assert task_payload["payload"] == {"mode": "nci"}
    assert task_payload["enqueue_payload"] == {"priority": 7}


def test_workflow_stage_to_dict_serializes_none_task_as_none() -> None:
    stage = WorkflowStage(
        stage_id="manual_gate",
        stage_kind="manual",
        status="planned",
    )

    payload = stage.to_dict()

    assert payload["task"] is None
    assert payload["input_artifacts"] == []
    assert payload["output_artifacts"] == []
    assert payload["metadata"] == {}


def test_workflow_task_from_raw_coerces_resource_request_and_empty_payload_defaults() -> None:
    task = WorkflowTask.from_raw(
        task_id="task_1",
        engine="",
        task_kind="",
        resource_request={"max_cores": "8", "bad": "not-int", "": 99},
        payload=None,
        enqueue_payload=None,
        submission_result=None,
        depends_on=["stage_a", "", "stage_b"],
        metadata=None,
    )

    payload = task.to_dict()

    assert payload["engine"] == "unknown"
    assert payload["task_kind"] == "task"
    assert payload["resource_request"] == {"max_cores": 8, "bad": 0}
    assert payload["payload"] == {}
    assert payload["enqueue_payload"] == {}
    assert payload["submission_result"] == {}
    assert payload["depends_on"] == ("stage_a", "stage_b")
    assert payload["metadata"] == {}


def test_coerce_workflow_plan_payload_preserves_raw_payload_shape() -> None:
    raw = {
        "workflow_id": "wf_loaded",
        "status": "running",
        "metadata": {"workspace_dir": "/tmp/wf_loaded"},
        "stages": [
            {
                "stage_id": "stage_1",
                "task": None,
                "metadata": {"custom": ["kept"]},
            }
        ],
        "custom_top_level": {"kept": True},
    }

    payload = coerce_workflow_plan_payload(raw)

    assert payload == raw
    assert payload["stages"] == raw["stages"]
    assert coerce_workflow_plan_payload(["not", "a", "mapping"]) == {}


def test_workflow_request_parameters_reads_only_the_canonical_mapping_path() -> None:
    parameters = {"max_orca_stages": 3}

    assert (
        workflow_request_parameters({"metadata": {"request": {"parameters": parameters}}})
        is parameters
    )
    assert workflow_request_parameters({"metadata": {"request": {"parameters": []}}}) == {}
    assert workflow_request_parameters({"metadata": "invalid"}) == {}


def test_workflow_metadata_repairs_missing_or_invalid_root_mapping() -> None:
    existing = {"metadata": {"kept": True}}
    assert workflow_metadata(existing) is existing["metadata"]

    for payload in ({}, {"metadata": "invalid"}):
        metadata = workflow_metadata(payload)
        assert metadata == {}
        assert payload["metadata"] is metadata


def test_required_route_line_fails_closed_on_missing_empty_or_blank_values() -> None:
    assert (
        required_route_line({"orca_route_line": "! r2scan-3c Opt TightSCF"}, "orca_route_line")
        == "! r2scan-3c Opt TightSCF"
    )

    for parameters in ({}, {"orca_route_line": ""}, {"orca_route_line": "   "}):
        with pytest.raises(ValueError, match="missing orca_route_line"):
            required_route_line(parameters, "orca_route_line")

    with pytest.raises(ValueError, match="missing orca_optts_route_line"):
        required_route_line({"orca_route_line": "! Opt"}, "orca_optts_route_line")


def test_workflow_template_request_to_dict_serializes_source_artifacts() -> None:
    request = WorkflowTemplateRequest(
        workflow_id="wf_2",
        template_name="conformer_screening",
        source_job_id="crest_1",
        source_job_type="crest_standard",
        reaction_key="mol_1",
        status="planned",
        requested_at="2026-05-20T00:00:00+00:00",
        parameters={"max_orca_stages": 3},
        source_artifacts=(WorkflowArtifactRef(kind="crest_best", path="/tmp/best.xyz"),),
    )

    payload = request.to_dict()

    assert payload["parameters"] == {"max_orca_stages": 3}
    assert payload["source_artifacts"] == [
        {"kind": "crest_best", "path": "/tmp/best.xyz", "selected": False, "metadata": {}}
    ]
