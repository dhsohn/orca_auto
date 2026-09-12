from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from orca_auto.flow.contracts import WorkflowStageWithTaskPayload
from orca_auto.flow.orchestration.requests import (
    ConformerScreeningWorkflowRequest,
    WorkflowCreationContext,
)
from orca_auto.flow.orchestration.template_builders import (
    _conformer_template_build,
)
from orca_auto.flow.orchestration.workflow_builders import (
    _ConformerWorkflowInput,
    _WorkflowWorkspace,
)


def _context(stage_calls: list[dict[str, Any]]) -> WorkflowCreationContext:
    def new_crest_stage(**kwargs: Any) -> WorkflowStageWithTaskPayload:
        stage_calls.append(kwargs)
        return cast(
            WorkflowStageWithTaskPayload,
            {
                "stage_id": kwargs["stage_id"],
                "stage_kind": "crest_stage",
                "status": "planned",
                "task": {"payload": dict(kwargs)},
                "metadata": {
                    "input_role": kwargs["input_role"],
                    "manifest_overrides": kwargs["manifest_overrides"],
                },
            },
        )

    return WorkflowCreationContext(
        workflow_id_factory=lambda: "wf_demo_generated",
        copy_input_fn=lambda source, target: str(target),
        now_utc_iso_fn=lambda: "2026-05-29T00:00:00+00:00",
        new_crest_stage_fn=new_crest_stage,
        write_workflow_payload_fn=lambda _workspace_dir, _payload: None,
        sync_workflow_registry_fn=lambda _root, _workspace_dir, _payload: None,
    )


def _workspace(tmp_path: Path) -> _WorkflowWorkspace:
    return _WorkflowWorkspace(
        workflow_id="wf_1",
        workflow_root_path=tmp_path,
        workspace_dir=tmp_path / "wf_1",
        requested_at="2026-05-29T00:00:00+00:00",
    )


def test_conformer_template_build_creates_single_molecule_stage(tmp_path: Path) -> None:
    stage_calls: list[dict[str, Any]] = []
    build = _conformer_template_build(
        ConformerScreeningWorkflowRequest(
            input_xyz="/unused/mol.xyz",
            workflow_root=tmp_path,
            crest_mode="standard",
            priority=8,
            max_cores=6,
            max_memory_gb=24,
            max_orca_stages=9,
            orca_route_line="! conformer",
            charge=1,
            multiplicity=3,
            crest_job_manifest={"ewin": 8},
        ),
        _workspace(tmp_path),
        _ConformerWorkflowInput(input_xyz="/copied/mol.xyz", reaction_key="mol"),
        _context(stage_calls),
    )

    assert build.request.template_name == "conformer_screening"
    assert build.request.reaction_key == "mol"
    assert build.request.parameters == {
        "crest_mode": "standard",
        "priority": 8,
        "max_cores": 6,
        "max_memory_gb": 24,
        "max_orca_stages": 9,
        "orca_route_line": "! conformer",
        "charge": 1,
        "multiplicity": 3,
        "crest_job_manifest": {"ewin": 8},
    }
    assert [artifact.path for artifact in build.request.source_artifacts] == ["/copied/mol.xyz"]
    assert [stage["stage_id"] for stage in build.stages] == ["crest_conformer_01"]
    assert stage_calls == [
        {
            "workflow_id": "wf_1",
            "template_name": "conformer_screening",
            "stage_id": "crest_conformer_01",
            "source_path": "/copied/mol.xyz",
            "input_role": "molecule",
            "mode": "standard",
            "priority": 8,
            "max_cores": 6,
            "max_memory_gb": 24,
            "manifest_overrides": {"charge": 1, "uhf": 2, "ewin": 8},
        }
    ]
