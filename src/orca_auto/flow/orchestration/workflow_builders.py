from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from orca_auto.core.commands.run_dir import assert_run_dir_publication_allowed
from orca_auto.core.engine_process import atomic_write_confined_bytes, ensure_confined_directory
from orca_auto.core.paths.workflow import (
    WORKFLOW_FILE_NAME,
    directory_is_workflow_scaffold,
    validate_workflow_id_path_segment,
)
from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.queue.publication import current_process_start_token, process_start_token
from orca_auto.core.utils import atomic_write_json
from orca_auto.core.utils.persistence import durable_mkdir, fsync_directory, load_json_mapping_file
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
from orca_auto.flow.xyz_utils import validated_xyz_atom_count

_REACTION_TS_SEARCH_CREST_MANIFEST_DEFAULTS: dict[str, Any] = {"rthr": 0.3}
_CREATION_MARKER = ".orca_auto_workflow_creation.json"


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
    if workspace_dir.is_symlink():
        raise ValueError(f"workflow workspace must not be a symlink: {workspace_dir}")
    workflow_file = workspace_dir / WORKFLOW_FILE_NAME
    if workflow_file.exists():
        raise FileExistsError(f"workflow already exists: {workflow_file}")
    if workspace_dir.exists():
        raise FileExistsError(f"workflow already exists: {workspace_dir}")


def _copy_input_impl(source: str, target: Path) -> str:
    src = Path(source).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Input XYZ not found: {src}")
    validated_xyz_atom_count(src)
    inputs_dir = next((parent for parent in target.parents if parent.name == "inputs"), None)
    if inputs_dir is None:
        raise ValueError("workflow input target must be inside a reserved inputs directory")
    trusted_workspace_root = inputs_dir.parent
    ensure_confined_directory(
        trusted_workspace_root,
        target.parent,
        label="workflow input directory",
    )
    atomic_write_confined_bytes(
        trusted_workspace_root,
        target,
        read_stable_regular_file(src),
        label="workflow materialized input",
    )
    return str(target.resolve())


def _resolved_source_inputs(*values: str | None) -> tuple[str, ...]:
    """Resolve the pre-copy source input paths recorded for provenance."""

    resolved: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text:
            resolved.append(str(Path(text).expanduser().resolve()))
    return tuple(resolved)


def _persist_workflow(
    *,
    persistence_context: WorkflowPersistenceContext,
    request: WorkflowTemplateRequest,
    stages: list[WorkflowStagePayload],
    creation_context: WorkflowCreationContext,
    source_inputs: tuple[str, ...],
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
            # The pre-copy source paths bind this workspace to the directory
            # it was materialized from (submission commit classification).
            "source_inputs": list(source_inputs),
        },
    )
    payload = plan.to_dict()
    payload["stages"] = cast(list[WorkflowStagePayload], list(stages))
    callback_payload = cast(dict[str, Any], payload)
    assert_run_dir_publication_allowed("workflow durable payload pre-commit")
    creation_context.write_workflow_payload_fn(persistence_context.workspace_dir, callback_payload)
    try:
        assert_run_dir_publication_allowed("workflow durable payload post-commit")
    except BaseException:
        try:
            (persistence_context.workspace_dir / WORKFLOW_FILE_NAME).unlink()
        except FileNotFoundError:
            pass
        else:
            fsync_directory(persistence_context.workspace_dir)
        raise
    creation_context.sync_workflow_registry_fn(
        persistence_context.workflow_root_path,
        persistence_context.workspace_dir,
        callback_payload,
    )
    try:
        (persistence_context.workspace_dir / _CREATION_MARKER).unlink()
    except FileNotFoundError:
        pass
    else:
        fsync_directory(persistence_context.workspace_dir)
    return payload


def _resolved_workspace_parent(
    workspace_parent: str | Path | None,
    *,
    workflow_root_path: Path,
) -> Path:
    """Validate the directory a new generation workspace is minted inside.

    Workspaces mirror the standalone ORCA layout: a submitted scaffold
    directory (a direct child of workflow_root) holds one generation
    directory per run. Direct API submissions without a scaffold mint the
    generation directly under workflow_root.
    """

    if workspace_parent is None:
        return workflow_root_path
    parent = Path(workspace_parent).expanduser().resolve()
    if parent == workflow_root_path:
        return parent
    if parent.parent != workflow_root_path:
        raise ValueError(
            "workflow scaffold must be a direct child of workflow_root "
            f"({workflow_root_path}); got: {parent}"
        )
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError(f"workflow scaffold is not a directory: {parent}")
    if (parent / WORKFLOW_FILE_NAME).exists():
        raise ValueError(f"workflow scaffold is already a workflow workspace: {parent}")
    if not directory_is_workflow_scaffold(parent):
        # Discovery and restart identify scaffold-hosted workspaces by the
        # scaffold manifest; a parent without one would strand the workspace.
        raise ValueError(f"workflow scaffold has no flow.yaml manifest: {parent}")
    return parent


def _workflow_workspace(
    *,
    workflow_id: str | None,
    workflow_root: str | Path,
    workspace_parent: str | Path | None,
    context: WorkflowCreationContext,
) -> _WorkflowWorkspace:
    raw_workflow_id = str(workflow_id or "").strip() or context.workflow_id_factory()
    resolved_workflow_id = _validate_workflow_id_path_segment(raw_workflow_id)
    workflow_root_path = Path(workflow_root).expanduser().resolve()
    durable_mkdir(workflow_root_path, parents=True, exist_ok=True)
    parent_dir = _resolved_workspace_parent(workspace_parent, workflow_root_path=workflow_root_path)
    if parent_dir != workflow_root_path and not is_visible_generation_name(resolved_workflow_id):
        raise ValueError(
            "a workflow workspace inside a scaffold must use a generation name "
            f"(YYYYMMDD-HHMMSS-<8hex>); got: {resolved_workflow_id!r}"
        )
    workspace_dir = (parent_dir / resolved_workflow_id).resolve()
    try:
        workspace_dir.relative_to(workflow_root_path)
    except ValueError as exc:
        raise ValueError(
            f"workflow_id must resolve under workflow_root: {resolved_workflow_id!r}"
        ) from exc
    with acquire_workflow_create_lock(workflow_root_path):
        if workspace_dir.exists() and not (workspace_dir / WORKFLOW_FILE_NAME).exists():
            marker = load_json_mapping_file(workspace_dir / _CREATION_MARKER) or {}
            owner_pid = marker.get("owner_pid")
            owner_start = str(marker.get("owner_process_start") or "")
            owner_is_current = (
                type(owner_pid) is int
                and owner_pid > 0
                and bool(owner_start)
                and process_start_token(owner_pid) == owner_start
            )
            if marker and not owner_is_current and not workspace_dir.is_symlink():
                shutil.rmtree(workspace_dir)
                fsync_directory(parent_dir)
        _ensure_new_workflow_workspace(workspace_dir)
        staging_dir = parent_dir / (f".{resolved_workflow_id}.creating-{secrets.token_hex(12)}")
        try:
            durable_mkdir(staging_dir, mode=0o700, exist_ok=False)
            atomic_write_json(
                staging_dir / _CREATION_MARKER,
                {
                    "workflow_id": resolved_workflow_id,
                    "owner_pid": os.getpid(),
                    "owner_process_start": current_process_start_token(),
                },
                ensure_ascii=True,
                indent=2,
            )
            staging_dir.rename(workspace_dir)
            fsync_directory(parent_dir)
        except BaseException:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
                fsync_directory(parent_dir)
            elif workspace_dir.exists() and not (workspace_dir / WORKFLOW_FILE_NAME).exists():
                shutil.rmtree(workspace_dir, ignore_errors=True)
                fsync_directory(parent_dir)
            raise
    return _WorkflowWorkspace(
        workflow_id=resolved_workflow_id,
        workflow_root_path=workflow_root_path,
        workspace_dir=workspace_dir,
        requested_at=context.now_utc_iso_fn(),
    )


def _cleanup_reserved_workflow_workspace(workspace: _WorkflowWorkspace) -> None:
    resolved_root = workspace.workflow_root_path.expanduser().resolve()
    path = workspace.workspace_dir
    if path.is_symlink():
        return
    if (path / WORKFLOW_FILE_NAME).exists():
        # Publication may already be durable even if registry sync reported an
        # ambiguous post-commit failure. Preserve the workflow for reindex/repair.
        return
    resolved_path = path.expanduser().resolve()
    if resolved_path.is_relative_to(resolved_root) and resolved_path != resolved_root:
        shutil.rmtree(resolved_path, ignore_errors=True)
        fsync_directory(resolved_path.parent)


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
    request: ConformerScreeningWorkflowRequest,
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


def _resolved_scan_ts_input(request: ScanTsSearchWorkflowRequest) -> _ConformerWorkflowInput:
    """Validate the scan input in place — no workspace ``inputs/`` copy.

    scan_ts_search materializes the geometry straight into its first ORCA
    stage at creation time, so an intermediate workspace copy would only
    duplicate the scaffold source.
    """
    src = Path(request.input_xyz).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Input XYZ not found: {src}")
    validated_xyz_atom_count(src)
    return _ConformerWorkflowInput(
        input_xyz=str(src),
        reaction_key=src.stem,
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
    "_resolved_scan_ts_input",
    "_cleanup_reserved_workflow_workspace",
    "_ensure_new_workflow_workspace",
    "_merge_manifest_defaults",
    "_optional_mapping_parameter",
    "_persist_workflow",
    "_persistence_context",
    "_validate_reaction_atom_sequence",
    "_validate_workflow_id_path_segment",
    "_workflow_workspace",
]
