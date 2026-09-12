from __future__ import annotations

from orca_auto.flow.contracts import WorkflowPlanPayload
from orca_auto.flow.orchestration.requests import (
    ConformerScreeningWorkflowRequest,
    WorkflowCreationContext,
)
from orca_auto.flow.orchestration.template_builders import _conformer_template_build
from orca_auto.flow.orchestration.workflow_builders import (
    _cleanup_reserved_workflow_workspace,
    _copy_conformer_input,
    _persist_workflow,
    _persistence_context,
    _resolved_source_inputs,
    _workflow_workspace,
)


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


__all__ = [
    "create_conformer_screening_workflow_impl",
]
