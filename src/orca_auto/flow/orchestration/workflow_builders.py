from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from orca_auto.core.paths.workflow import (
    WORKFLOW_FILE_NAME,
    validate_workflow_id_path_segment,
)
from orca_auto.flow.contracts import (
    WorkflowPlan,
    WorkflowPlanPayload,
    WorkflowStagePayload,
    WorkflowTemplateRequest,
)
from orca_auto.flow.orchestration.requests import (
    ConformerScreeningWorkflowRequest,
    ReactionTsSearchWorkflowCreationContext,
    ReactionTsSearchWorkflowRequest,
    ScanTsSearchWorkflowRequest,
    WorkflowCreationContext,
    WorkflowPersistenceContext,
)
from orca_auto.flow.workflow.store import acquire_workflow_create_lock

_REACTION_TS_SEARCH_CREST_MANIFEST_DEFAULTS: dict[str, Any] = {"rthr": 0.3}


@dataclass(frozen=True)
class _WorkflowWorkspace:
    workflow_id: str
    workflow_root_path: Path
    workspace_dir: Path
    requested_at: str


@dataclass(frozen=True)
class _ReactionWorkflowInputs:
    reactant_xyz: str
    product_xyz: str
    reaction_key: str


@dataclass(frozen=True)
class _ConformerWorkflowInput:
    input_xyz: str
    reaction_key: str


def _merge_manifest_defaults(
    defaults: dict[str, Any],
    overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(defaults)
    for raw_key, value in dict(overrides or {}).items():
        key = str(raw_key).strip()
        if not key:
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            merged.pop(key, None)
            continue
        merged[key] = value
    return merged


def _optional_mapping_parameter(name: str, value: dict[str, Any] | None) -> dict[str, Any]:
    return {name: dict(value)} if value else {}


def _validate_workflow_id_path_segment(value: Any) -> str:
    return validate_workflow_id_path_segment(value)


def _ensure_new_workflow_workspace(workspace_dir: Path) -> None:
    workflow_file = workspace_dir / WORKFLOW_FILE_NAME
    if workflow_file.exists():
        raise FileExistsError(f"workflow already exists: {workflow_file}")


def _copy_input_impl(source: str, target: Path) -> str:
    src = Path(source).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Input XYZ not found: {src}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    return str(target.resolve())


def _persist_workflow(
    *,
    persistence_context: WorkflowPersistenceContext,
    request: WorkflowTemplateRequest,
    stages: list[WorkflowStagePayload],
    creation_context: WorkflowCreationContext,
) -> WorkflowPlanPayload:
    plan = WorkflowPlan(
        workflow_id=persistence_context.workflow_id,
        template_name=persistence_context.template_name,
        status="planned",
        source_job_id=persistence_context.source_job_id,
        source_job_type=persistence_context.source_job_type,
        reaction_key=persistence_context.reaction_key,
        requested_at=persistence_context.requested_at,
        stages=(),
        metadata={
            "request": request.to_dict(),
            "workspace_dir": str(persistence_context.workspace_dir),
        },
    )
    payload = plan.to_dict()
    payload["stages"] = cast(list[WorkflowStagePayload], list(stages))
    callback_payload = cast(dict[str, Any], payload)
    with acquire_workflow_create_lock(persistence_context.workflow_root_path):
        _ensure_new_workflow_workspace(persistence_context.workspace_dir)
        creation_context.write_workflow_payload_fn(
            persistence_context.workspace_dir, callback_payload
        )
        creation_context.sync_workflow_registry_fn(
            persistence_context.workflow_root_path,
            persistence_context.workspace_dir,
            callback_payload,
        )
    return payload


def _workflow_workspace(
    *,
    workflow_id: str | None,
    workflow_root: str | Path,
    default_id_prefix: str,
    context: WorkflowCreationContext,
) -> _WorkflowWorkspace:
    raw_workflow_id = str(workflow_id or "").strip() or context.workflow_id_factory(
        default_id_prefix
    )
    resolved_workflow_id = _validate_workflow_id_path_segment(raw_workflow_id)
    workflow_root_path = Path(workflow_root).expanduser().resolve()
    workspace_dir = (workflow_root_path / resolved_workflow_id).resolve()
    try:
        workspace_dir.relative_to(workflow_root_path)
    except ValueError as exc:
        raise ValueError(
            f"workflow_id must resolve under workflow_root: {resolved_workflow_id!r}"
        ) from exc
    _ensure_new_workflow_workspace(workspace_dir)
    return _WorkflowWorkspace(
        workflow_id=resolved_workflow_id,
        workflow_root_path=workflow_root_path,
        workspace_dir=workspace_dir,
        requested_at=context.now_utc_iso_fn(),
    )


def _validate_reaction_atom_sequence(
    request: ReactionTsSearchWorkflowRequest,
    context: ReactionTsSearchWorkflowCreationContext,
) -> None:
    reactant_sequence = context.load_xyz_atom_sequence_fn(request.reactant_xyz)
    product_sequence = context.load_xyz_atom_sequence_fn(request.product_xyz)
    if reactant_sequence == product_sequence:
        return
    raise ValueError(
        "reaction_ts_search requires identical reactant/product atom order for xTB path search; "
        f"reactant sequence={list(reactant_sequence)}, product sequence={list(product_sequence)}"
    )


def _copy_reaction_inputs(
    request: ReactionTsSearchWorkflowRequest,
    workspace: _WorkflowWorkspace,
    context: WorkflowCreationContext,
) -> _ReactionWorkflowInputs:
    input_reactant = context.copy_input_fn(
        request.reactant_xyz,
        workspace.workspace_dir / "inputs" / "reactants" / Path(request.reactant_xyz).name,
    )
    input_product = context.copy_input_fn(
        request.product_xyz,
        workspace.workspace_dir / "inputs" / "products" / Path(request.product_xyz).name,
    )
    return _ReactionWorkflowInputs(
        reactant_xyz=input_reactant,
        product_xyz=input_product,
        reaction_key=f"{Path(input_reactant).stem}_to_{Path(input_product).stem}",
    )


def _copy_conformer_input(
    request: ConformerScreeningWorkflowRequest | ScanTsSearchWorkflowRequest,
    workspace: _WorkflowWorkspace,
    context: WorkflowCreationContext,
) -> _ConformerWorkflowInput:
    copied_input = context.copy_input_fn(
        request.input_xyz,
        workspace.workspace_dir / "inputs" / Path(request.input_xyz).name,
    )
    return _ConformerWorkflowInput(
        input_xyz=copied_input,
        reaction_key=Path(copied_input).stem,
    )


def _persistence_context(
    workspace: _WorkflowWorkspace,
    template_request: WorkflowTemplateRequest,
) -> WorkflowPersistenceContext:
    return WorkflowPersistenceContext(
        workflow_root_path=workspace.workflow_root_path,
        workspace_dir=workspace.workspace_dir,
        workflow_id=workspace.workflow_id,
        template_name=template_request.template_name,
        source_job_id=template_request.source_job_id,
        source_job_type=template_request.source_job_type,
        reaction_key=template_request.reaction_key,
        requested_at=workspace.requested_at,
    )


__all__ = [
    "_ConformerWorkflowInput",
    "_REACTION_TS_SEARCH_CREST_MANIFEST_DEFAULTS",
    "_ReactionWorkflowInputs",
    "_WorkflowWorkspace",
    "_copy_conformer_input",
    "_copy_input_impl",
    "_copy_reaction_inputs",
    "_ensure_new_workflow_workspace",
    "_merge_manifest_defaults",
    "_optional_mapping_parameter",
    "_persist_workflow",
    "_persistence_context",
    "_validate_reaction_atom_sequence",
    "_validate_workflow_id_path_segment",
    "_workflow_workspace",
]
