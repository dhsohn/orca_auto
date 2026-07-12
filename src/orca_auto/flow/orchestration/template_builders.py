from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from orca_auto.flow._orca_stage_materialization import build_materialized_orca_stage
from orca_auto.flow.contracts import (
    WorkflowArtifactRef,
    WorkflowStageInput,
    WorkflowStagePayload,
    WorkflowTemplateRequest,
)
from orca_auto.flow.orchestration.charge_spin import (
    manifest_with_charge_spin as _manifest_with_charge_spin,
)
from orca_auto.flow.orchestration.requests import (
    ConformerScreeningWorkflowRequest,
    ReactionTsSearchWorkflowRequest,
    ScanTsSearchWorkflowRequest,
    WorkflowCreationContext,
)
from orca_auto.flow.orchestration.workflow_builders import (
    _ConformerWorkflowInput,
    _optional_mapping_parameter,
    _ReactionWorkflowInputs,
    _WorkflowWorkspace,
)
from orca_auto.flow.state import workflow_workspace_internal_engine_paths


def scan_geom_block(scan_coordinate: str) -> str:
    return "\n".join(
        [
            "%geom",
            "  Scan",
            f"    {scan_coordinate.strip()}",
            "  end",
            "end",
        ]
    )


@dataclass(frozen=True)
class _WorkflowTemplateBuild:
    request: WorkflowTemplateRequest
    stages: list[WorkflowStagePayload]


def _crest_stage_payload(
    context: WorkflowCreationContext,
    *,
    workflow_id: str,
    template_name: str,
    stage_id: str,
    source_path: str,
    input_role: str,
    mode: str,
    priority: int,
    max_cores: int,
    max_memory_gb: int,
    manifest_overrides: dict[str, Any] | None,
) -> WorkflowStagePayload:
    return cast(
        WorkflowStagePayload,
        context.new_crest_stage_fn(
            workflow_id=workflow_id,
            template_name=template_name,
            stage_id=stage_id,
            source_path=source_path,
            input_role=input_role,
            mode=mode,
            priority=priority,
            max_cores=max_cores,
            max_memory_gb=max_memory_gb,
            manifest_overrides=manifest_overrides,
        ),
    )


def _reaction_crest_stages(
    request: ReactionTsSearchWorkflowRequest,
    workspace: _WorkflowWorkspace,
    inputs: _ReactionWorkflowInputs,
    context: WorkflowCreationContext,
    *,
    manifest_overrides: dict[str, Any],
) -> list[WorkflowStagePayload]:
    return [
        _crest_stage_payload(
            context,
            workflow_id=workspace.workflow_id,
            template_name="reaction_ts_search",
            stage_id="crest_reactant_01",
            source_path=inputs.reactant_xyz,
            input_role="reactant",
            mode=request.crest_mode,
            priority=request.priority,
            max_cores=request.max_cores,
            max_memory_gb=request.max_memory_gb,
            manifest_overrides=_manifest_with_charge_spin(
                charge=request.charge,
                multiplicity=request.multiplicity,
                manifest_overrides=manifest_overrides,
            ),
        ),
        _crest_stage_payload(
            context,
            workflow_id=workspace.workflow_id,
            template_name="reaction_ts_search",
            stage_id="crest_product_01",
            source_path=inputs.product_xyz,
            input_role="product",
            mode=request.crest_mode,
            priority=request.priority,
            max_cores=request.max_cores,
            max_memory_gb=request.max_memory_gb,
            manifest_overrides=_manifest_with_charge_spin(
                charge=request.charge,
                multiplicity=request.multiplicity,
                manifest_overrides=manifest_overrides,
            ),
        ),
    ]


def _conformer_crest_stages(
    request: ConformerScreeningWorkflowRequest,
    workspace: _WorkflowWorkspace,
    copied_input: _ConformerWorkflowInput,
    context: WorkflowCreationContext,
) -> list[WorkflowStagePayload]:
    return [
        _crest_stage_payload(
            context,
            workflow_id=workspace.workflow_id,
            template_name="conformer_screening",
            stage_id="crest_conformer_01",
            source_path=copied_input.input_xyz,
            input_role="molecule",
            mode=request.crest_mode,
            priority=request.priority,
            max_cores=request.max_cores,
            max_memory_gb=request.max_memory_gb,
            manifest_overrides=_manifest_with_charge_spin(
                charge=request.charge,
                multiplicity=request.multiplicity,
                manifest_overrides=request.crest_job_manifest,
            ),
        ),
    ]


def _reaction_template_request(
    request: ReactionTsSearchWorkflowRequest,
    workspace: _WorkflowWorkspace,
    inputs: _ReactionWorkflowInputs,
    *,
    resolved_crest_job_manifest: dict[str, Any],
) -> WorkflowTemplateRequest:
    return WorkflowTemplateRequest(
        workflow_id=workspace.workflow_id,
        template_name="reaction_ts_search",
        source_job_id=request.source_job_id,
        source_job_type=request.source_job_type or "raw_xyz",
        reaction_key=inputs.reaction_key,
        status="planned",
        requested_at=workspace.requested_at,
        parameters={
            "crest_mode": request.crest_mode,
            "priority": int(request.priority),
            "max_cores": int(request.max_cores),
            "max_memory_gb": int(request.max_memory_gb),
            "max_crest_candidates": int(request.max_crest_candidates),
            "max_xtb_stages": int(request.max_xtb_stages),
            "max_xtb_handoff_retries": int(request.max_xtb_handoff_retries),
            "max_orca_stages": int(request.max_orca_stages),
            "orca_route_line": str(request.orca_route_line),
            "charge": int(request.charge),
            "multiplicity": int(request.multiplicity),
            **_optional_mapping_parameter("crest_job_manifest", resolved_crest_job_manifest),
            **_optional_mapping_parameter("xtb_job_manifest", request.xtb_job_manifest),
            **_optional_mapping_parameter("endpoint_pairing", request.endpoint_pairing),
        },
        source_artifacts=(
            WorkflowArtifactRef(kind="reactant_xyz", path=inputs.reactant_xyz, selected=True),
            WorkflowArtifactRef(kind="product_xyz", path=inputs.product_xyz, selected=True),
        ),
    )


def _conformer_template_request(
    request: ConformerScreeningWorkflowRequest,
    workspace: _WorkflowWorkspace,
    copied_input: _ConformerWorkflowInput,
) -> WorkflowTemplateRequest:
    return WorkflowTemplateRequest(
        workflow_id=workspace.workflow_id,
        template_name="conformer_screening",
        source_job_id="",
        source_job_type="raw_xyz",
        reaction_key=copied_input.reaction_key,
        status="planned",
        requested_at=workspace.requested_at,
        parameters={
            "crest_mode": request.crest_mode,
            "priority": int(request.priority),
            "max_cores": int(request.max_cores),
            "max_memory_gb": int(request.max_memory_gb),
            "max_orca_stages": int(request.max_orca_stages),
            "orca_route_line": str(request.orca_route_line),
            "charge": int(request.charge),
            "multiplicity": int(request.multiplicity),
            **(
                {"boltzmann_temperature_k": request.boltzmann_temperature_k}
                if request.boltzmann_temperature_k is not None
                else {}
            ),
            **_optional_mapping_parameter("crest_job_manifest", request.crest_job_manifest),
            **_optional_mapping_parameter("interaction_energy", request.interaction_energy),
            **_optional_mapping_parameter("rmsd_dedup", request.rmsd_dedup),
        },
        source_artifacts=(
            WorkflowArtifactRef(kind="input_xyz", path=copied_input.input_xyz, selected=True),
        ),
    )


def _reaction_template_build(
    request: ReactionTsSearchWorkflowRequest,
    workspace: _WorkflowWorkspace,
    inputs: _ReactionWorkflowInputs,
    context: WorkflowCreationContext,
    *,
    resolved_crest_job_manifest: dict[str, Any],
) -> _WorkflowTemplateBuild:
    return _WorkflowTemplateBuild(
        request=_reaction_template_request(
            request,
            workspace,
            inputs,
            resolved_crest_job_manifest=resolved_crest_job_manifest,
        ),
        stages=_reaction_crest_stages(
            request,
            workspace,
            inputs,
            context,
            manifest_overrides=resolved_crest_job_manifest,
        ),
    )


def _conformer_template_build(
    request: ConformerScreeningWorkflowRequest,
    workspace: _WorkflowWorkspace,
    copied_input: _ConformerWorkflowInput,
    context: WorkflowCreationContext,
) -> _WorkflowTemplateBuild:
    return _WorkflowTemplateBuild(
        request=_conformer_template_request(request, workspace, copied_input),
        stages=_conformer_crest_stages(request, workspace, copied_input, context),
    )


def _scan_ts_scan_stage(
    request: ScanTsSearchWorkflowRequest,
    workspace: _WorkflowWorkspace,
    copied_input: _ConformerWorkflowInput,
) -> WorkflowStagePayload:
    orca_paths = workflow_workspace_internal_engine_paths(workspace.workspace_dir, engine="orca")
    candidate = WorkflowStageInput(
        source_job_id="",
        source_job_type="raw_xyz",
        reaction_key=copied_input.reaction_key,
        selected_input_xyz=copied_input.input_xyz,
        rank=1,
        kind="scan_input",
        artifact_path=copied_input.input_xyz,
        selected=True,
        score=0.0,
        metadata={"input_role": "molecule"},
    )
    stage = build_materialized_orca_stage(
        workflow_id=workspace.workflow_id,
        template_name="scan_ts_search",
        stage_id="orca_scan_01",
        stage_key="01_scan",
        stage_root_name="",
        workspace_dir=orca_paths["allowed_root"],
        input_artifact_kind="input_xyz",
        candidate=candidate,
        task_kind="relaxed_scan",
        route_line=str(request.orca_route_line),
        charge=int(request.charge),
        multiplicity=int(request.multiplicity),
        max_cores=int(request.max_cores),
        max_memory_gb=int(request.max_memory_gb),
        priority=int(request.priority),
        xyz_filename="scan_input.xyz",
        inp_filename="scan.inp",
        geom_block=scan_geom_block(request.scan_coordinate),
    )
    stage_dict = stage.to_dict()
    # The relaxed scan is a prerequisite, not a TS candidate: its failure must
    # fail the whole workflow (the status reducer otherwise treats a failed
    # ORCA stage as a terminal candidate outcome, not a workflow failure).
    stage_metadata = stage_dict.setdefault("metadata", {})
    if isinstance(stage_metadata, dict):
        stage_metadata["workflow_fatal"] = True
        stage_metadata["scan_direction"] = "forward"
        stage_metadata["scan_coordinate"] = str(request.scan_coordinate).strip()
    return cast(WorkflowStagePayload, stage_dict)


def _scan_ts_template_request(
    request: ScanTsSearchWorkflowRequest,
    workspace: _WorkflowWorkspace,
    copied_input: _ConformerWorkflowInput,
) -> WorkflowTemplateRequest:
    return WorkflowTemplateRequest(
        workflow_id=workspace.workflow_id,
        template_name="scan_ts_search",
        source_job_id="",
        source_job_type="raw_xyz",
        reaction_key=copied_input.reaction_key,
        status="planned",
        requested_at=workspace.requested_at,
        parameters={
            "priority": int(request.priority),
            "max_cores": int(request.max_cores),
            "max_memory_gb": int(request.max_memory_gb),
            "max_orca_stages": int(request.max_orca_stages),
            "max_scan_extensions": int(request.max_scan_extensions),
            "orca_route_line": str(request.orca_route_line),
            "orca_optts_route_line": str(request.orca_optts_route_line),
            "scan_coordinate": str(request.scan_coordinate),
            "barrier_threshold_kcal": float(request.barrier_threshold_kcal),
            "charge": int(request.charge),
            "multiplicity": int(request.multiplicity),
        },
        source_artifacts=(
            WorkflowArtifactRef(kind="input_xyz", path=copied_input.input_xyz, selected=True),
        ),
    )


def _scan_ts_template_build(
    request: ScanTsSearchWorkflowRequest,
    workspace: _WorkflowWorkspace,
    copied_input: _ConformerWorkflowInput,
) -> _WorkflowTemplateBuild:
    return _WorkflowTemplateBuild(
        request=_scan_ts_template_request(request, workspace, copied_input),
        stages=[_scan_ts_scan_stage(request, workspace, copied_input)],
    )


__all__ = [
    "_WorkflowTemplateBuild",
    "_conformer_template_build",
    "_conformer_template_request",
    "_reaction_template_build",
    "_reaction_template_request",
    "_scan_ts_template_build",
    "scan_geom_block",
]
