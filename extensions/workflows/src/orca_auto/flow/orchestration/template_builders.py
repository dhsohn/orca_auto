from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from orca_auto.flow.contracts import (
    WorkflowArtifactRef,
    WorkflowStagePayload,
    WorkflowTemplateRequest,
)
from orca_auto.flow.orchestration.charge_spin import (
    manifest_with_charge_spin as _manifest_with_charge_spin,
)
from orca_auto.flow.orchestration.charge_spin import (
    strict_int,
)
from orca_auto.flow.orchestration.requests import (
    ConformerScreeningWorkflowRequest,
    WorkflowCreationContext,
)
from orca_auto.flow.orchestration.workflow_builders import (
    _ConformerWorkflowInput,
    _optional_mapping_parameter,
    _WorkflowWorkspace,
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
            "charge": strict_int(request.charge, field="charge"),
            "multiplicity": strict_int(request.multiplicity, field="multiplicity", minimum=1),
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


__all__ = [
    "_WorkflowTemplateBuild",
    "_conformer_template_build",
    "_conformer_template_request",
]
