from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.utils import now_utc_iso

from ..registry import append_workflow_journal_event
from ..state import acquire_workflow_lock, load_workflow_payload
from .mutation import (
    WorkflowRestartMutation,
    _apply_restart_summary,
    _build_restart_mutation,
    _reset_restartable_stages,
    _restart_paths,
    _validate_restart_request,
)
from .settings import _flow_restart_settings


def restart_failed_workflow(
    *,
    workspace_dir: str | Path,
    workflow_root: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    workspace, root = _restart_paths(workspace_dir=workspace_dir, workflow_root=workflow_root)

    with acquire_workflow_lock(workspace):
        payload = load_workflow_payload(workspace)
        previous_status, workflow_id = _validate_restart_request(
            payload,
            workspace=workspace,
            force=bool(force),
        )
        flow_settings = _flow_restart_settings(workspace, payload)
        restarted_stages = _reset_restartable_stages(payload, flow_settings=flow_settings)

        if not restarted_stages:
            raise ValueError(
                f"workflow has no failed or cancelled stages to restart: {workflow_id}"
            )

        restarted_at = now_utc_iso()
        _apply_restart_summary(
            payload,
            previous_status=previous_status,
            restarted_at=restarted_at,
            restarted_stages=restarted_stages,
            flow_settings=flow_settings,
        )
        mutation = _build_restart_mutation(
            root=root,
            workspace=workspace,
            payload=payload,
            previous_status=previous_status,
            restarted_at=restarted_at,
            restarted_stages=restarted_stages,
            flow_settings=flow_settings,
        )

    append_workflow_journal_event(
        mutation.root,
        event_type="workflow_restarted",
        workflow_id=mutation.workflow_id,
        template_name=mutation.template_name,
        previous_status=mutation.previous_status,
        status="planned",
        reason="run_dir_restart",
        metadata=mutation.journal_metadata(),
    )
    return mutation.response_payload()


__all__ = ["WorkflowRestartMutation", "restart_failed_workflow"]
