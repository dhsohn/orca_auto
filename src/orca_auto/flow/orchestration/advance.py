from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.paths.workflow import validate_workflow_workspace_identity
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
from orca_auto.flow.workflow.si import write_workflow_si


def _workflow_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    metadata = {}
    payload["metadata"] = metadata
    return metadata


def _validate_or_quarantine_workflow_identity(
    payload: dict[str, Any],
    *,
    workspace_dir: Path,
    workflow_root_path: Path,
    deps: OrchestrationDeps,
) -> str:
    metadata = _workflow_metadata(payload)
    workflow_error = metadata.get("workflow_error")
    if (
        payload.get("status") == "failed"
        and isinstance(workflow_error, dict)
        and workflow_error.get("scope") == "workflow_identity_validation"
    ):
        # The quarantine marker keeps this workflow in sync-only mode. Let
        # terminal-sync passes cancel and drain active children without
        # re-raising the same identity error forever.
        return str(payload.get("workflow_id") or "").strip()
    try:
        return validate_workflow_workspace_identity(
            workspace_dir,
            payload.get("workflow_id"),
        )
    except ValueError as exc:
        reason = str(exc)
        payload["status"] = "failed"
        metadata["workflow_error"] = {
            "status": "failed",
            "scope": "workflow_identity_validation",
            "reason": reason,
            "message": reason,
            "detected_at": deps.persistence.now_utc_iso(),
        }
        deps.persistence.write_workflow_payload(workspace_dir, payload)
        deps.persistence.sync_workflow_registry(workflow_root_path, workspace_dir, payload)
        write_workflow_html_report(workspace_dir, payload)
        # Persist the quarantine before any child sync, then continue in
        # sync-only mode. This blocks new submissions while allowing the
        # normal finalization path to cancel and drain active children.
        return str(payload.get("workflow_id") or "").strip()


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
    config = engine_options or WorkflowEngineOptions.from_values(
        crest_config=crest_config,
        xtb_config=xtb_config,
        orca_config=orca_config,
        orca_repo_root=orca_repo_root,
    )
    workspace_dir = o.persistence.resolve_workflow_workspace(
        target=target,
        workflow_root=workflow_root_path,
    )
    with o.persistence.acquire_workflow_lock(workspace_dir):
        payload = o.persistence.load_workflow_payload(workspace_dir)
        workflow_id = _validate_or_quarantine_workflow_identity(
            payload,
            workspace_dir=workspace_dir,
            workflow_root_path=workflow_root_path,
            deps=o,
        )
        sync_only = o.stages.workflow._workflow_sync_only(payload)
        context = _AdvanceContext(
            deps=o,
            workflow_root_path=workflow_root_path,
            workspace_dir=workspace_dir,
            workflow_id=workflow_id,
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
        write_workflow_si(workspace_dir, payload)
        return payload


__all__ = [
    "advance_workflow",
    "cancel_materialized_workflow",
]
