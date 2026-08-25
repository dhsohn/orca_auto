from __future__ import annotations

from orca_auto.flow.contracts import WorkflowPlanPayload
from orca_auto.flow.orchestration.requests import (
    ConformerScreeningWorkflowRequest,
    ReactionTsSearchWorkflowCreationContext,
    ReactionTsSearchWorkflowRequest,
    ScanTsSearchWorkflowRequest,
    WorkflowCreationContext,
)
from orca_auto.flow.orchestration.template_builders import (
    _conformer_template_build,
    _reaction_template_build,
    _scan_ts_template_build,
)
from orca_auto.flow.orchestration.workflow_builders import (
    _REACTION_TS_SEARCH_CREST_MANIFEST_DEFAULTS,
    _cleanup_reserved_workflow_workspace,
    _copy_conformer_input,
    _copy_reaction_inputs,
    _merge_manifest_defaults,
    _persist_workflow,
    _persistence_context,
    _resolved_scan_ts_input,
    _resolved_source_inputs,
    _validate_reaction_atom_sequence,
    _workflow_workspace,
)


def create_reaction_ts_search_workflow_impl(
    *,
    request: ReactionTsSearchWorkflowRequest,
    context: ReactionTsSearchWorkflowCreationContext,
) -> WorkflowPlanPayload:
    workspace = _workflow_workspace(
        workflow_id=request.workflow_id,
        workflow_root=request.workflow_root,
        workspace_parent=request.scaffold_dir,
        context=context,
    )
    try:
        resolved_crest_job_manifest = _merge_manifest_defaults(
            _REACTION_TS_SEARCH_CREST_MANIFEST_DEFAULTS,
            request.crest_job_manifest,
        )
        _validate_reaction_atom_sequence(request, context)
        inputs = _copy_reaction_inputs(request, workspace, context)
        template_build = _reaction_template_build(
            request,
            workspace,
            inputs,
            context,
            resolved_crest_job_manifest=resolved_crest_job_manifest,
        )
        return _persist_workflow(
            persistence_context=_persistence_context(workspace, template_build.request),
            request=template_build.request,
            stages=template_build.stages,
            creation_context=context,
            source_inputs=_resolved_source_inputs(request.reactant_xyz, request.product_xyz),
        )
    except BaseException:
        _cleanup_reserved_workflow_workspace(workspace)
        raise


def create_conformer_screening_workflow_impl(
    *,
    request: ConformerScreeningWorkflowRequest,
    context: WorkflowCreationContext,
) -> WorkflowPlanPayload:
    workspace = _workflow_workspace(
        workflow_id=request.workflow_id,
        workflow_root=request.workflow_root,
        workspace_parent=request.scaffold_dir,
        context=context,
    )
    try:
        copied_input = _copy_conformer_input(request, workspace, context)
        template_build = _conformer_template_build(request, workspace, copied_input, context)
        return _persist_workflow(
            persistence_context=_persistence_context(workspace, template_build.request),
            request=template_build.request,
            stages=template_build.stages,
            creation_context=context,
            source_inputs=_resolved_source_inputs(request.input_xyz),
        )
    except BaseException:
        _cleanup_reserved_workflow_workspace(workspace)
        raise


def create_scan_ts_search_workflow_impl(
    *,
    request: ScanTsSearchWorkflowRequest,
    context: WorkflowCreationContext,
) -> WorkflowPlanPayload:
    workspace = _workflow_workspace(
        workflow_id=request.workflow_id,
        workflow_root=request.workflow_root,
        workspace_parent=request.scaffold_dir,
        context=context,
    )
    try:
        source_input = _resolved_scan_ts_input(request)
        template_build = _scan_ts_template_build(request, workspace, source_input)
        return _persist_workflow(
            persistence_context=_persistence_context(workspace, template_build.request),
            request=template_build.request,
            stages=template_build.stages,
            creation_context=context,
            source_inputs=_resolved_source_inputs(request.input_xyz),
        )
    except BaseException:
        _cleanup_reserved_workflow_workspace(workspace)
        raise


__all__ = [
    "create_conformer_screening_workflow_impl",
    "create_reaction_ts_search_workflow_impl",
    "create_scan_ts_search_workflow_impl",
]
