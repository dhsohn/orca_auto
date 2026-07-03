from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.flow.engine_options import WorkflowEngineOptions
from orca_auto.flow.orchestration.advance_phases import (
    AdvanceContext as _AdvanceContext,
)
from orca_auto.flow.orchestration.advance_phases import (
    _advance_phases,
    _finalize_advanced_workflow,
    _run_advance_phase,
)
from orca_auto.flow.orchestration.dep_types import OrchestrationDeps
from orca_auto.flow.orchestration.deps import (
    orchestration_context as _orchestration_context,
)
from orca_auto.flow.orchestration.workflow_cancellation import (
    cancel_materialized_workflow,
)
from orca_auto.flow.workflow.report import write_workflow_html_report


def advance_workflow(
    *,
    target: str,
    workflow_root: str | Path,
    crest_config: str | None = None,
    xtb_config: str | None = None,
    orca_config: str | None = None,
    orca_repo_root: str | None = None,
    engine_options: WorkflowEngineOptions | None = None,
    submit_ready: bool = True,
    deps: OrchestrationDeps | None = None,
) -> dict[str, Any]:
    o = _orchestration_context(deps)
    workflow_root_path = Path(workflow_root).expanduser().resolve()
    workspace_dir = o.persistence.resolve_workflow_workspace(
        target=target,
        workflow_root=workflow_root_path,
    )
    with o.persistence.acquire_workflow_lock(workspace_dir):
        payload = o.persistence.load_workflow_payload(workspace_dir)
        sync_only = o.stages.workflow._workflow_sync_only(payload)
        config = engine_options or WorkflowEngineOptions.from_values(
            crest_config=crest_config,
            xtb_config=xtb_config,
            orca_config=orca_config,
            orca_repo_root=orca_repo_root,
        )
        context = _AdvanceContext(
            deps=o,
            workflow_root_path=workflow_root_path,
            workspace_dir=workspace_dir,
            workflow_id=o.stages.support._normalize_text(payload.get("workflow_id")),
            template_name=o.stages.support._normalize_text(payload.get("template_name")),
            sync_only=sync_only,
            submit_ready=bool(submit_ready) and not sync_only,
        )
        for phase in _advance_phases(config):
            _run_advance_phase(payload, context, phase)

        _finalize_advanced_workflow(payload, context, config)
        o.persistence.write_workflow_payload(workspace_dir, payload)
        o.persistence.sync_workflow_registry(workflow_root_path, workspace_dir, payload)
        write_workflow_html_report(workspace_dir, payload)
        return payload


__all__ = [
    "advance_workflow",
    "cancel_materialized_workflow",
]
