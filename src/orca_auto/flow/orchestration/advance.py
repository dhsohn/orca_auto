from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from orca_auto.core.paths.workflow import validate_workflow_workspace_identity
from orca_auto.core.utils import parse_iso_utc
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

logger = logging.getLogger(__name__)

_SI_PUBLISH_MAX_ATTEMPTS = 5
_SI_PUBLISH_RETRY_BASE_SECONDS = 30
_SI_PUBLISH_RETRY_MAX_SECONDS = 30 * 60


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
        isinstance(workflow_error, dict)
        and workflow_error.get("scope") == "workflow_identity_validation"
    ):
        try:
            recovered_workflow_id = validate_workflow_workspace_identity(
                workspace_dir,
                payload.get("workflow_id"),
            )
        except ValueError:
            recovered_workflow_id = ""
        if recovered_workflow_id:
            # The operator restored the durable ID/workspace invariant. Clear
            # the stale quarantine marker while retaining the terminal status;
            # the ordinary checkpoint below persists and reindexes recovery.
            metadata.pop("workflow_error", None)
            return recovered_workflow_id
        if payload.get("status") == "failed":
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
        metadata = _workflow_metadata(payload)
        was_pending = bool(metadata.get("si_publish_pending"))
        was_blocked = bool(metadata.get("si_publish_blocked"))
        try:
            previous_attempts = max(0, int(metadata.get("si_publish_attempts", 0)))
        except (TypeError, ValueError):
            previous_attempts = 0
        now = parse_iso_utc(o.persistence.now_utc_iso())
        retry_at = parse_iso_utc(metadata.get("si_publish_next_retry_at"))
        if was_blocked or (
            was_pending and retry_at is not None and now is not None and retry_at > now
        ):
            # Engine reconciliation may continue, but a blocked/not-yet-due SI
            # publication is not retried by ordinary worker cycles.
            o.persistence.write_workflow_payload(workspace_dir, payload)
            o.persistence.sync_workflow_registry(workflow_root_path, workspace_dir, payload)
            write_workflow_html_report(workspace_dir, payload)
            return payload
        attempts = previous_attempts + 1
        generation = str(metadata.get("last_advanced_at") or "").strip()
        metadata["si_publish_pending"] = True
        metadata["si_publish_generation"] = generation
        metadata.pop("si_publish_next_retry_at", None)
        o.persistence.write_workflow_payload(workspace_dir, payload)
        o.persistence.sync_workflow_registry(workflow_root_path, workspace_dir, payload)
        write_workflow_html_report(workspace_dir, payload)
        # Count the SI writer attempt in authoritative workflow.json only after
        # registry discoverability is checkpointed. Registry lag/failure must
        # not consume the writer's crash budget.
        metadata["si_publish_attempts"] = attempts
        o.persistence.write_workflow_payload(workspace_dir, payload)
        if attempts > _SI_PUBLISH_MAX_ATTEMPTS:
            metadata["si_publish_pending"] = False
            metadata["si_publish_blocked"] = True
            metadata["si_publish_error"] = "SI publication attempt budget exhausted"
            o.persistence.write_workflow_payload(workspace_dir, payload)
            o.persistence.sync_workflow_registry(workflow_root_path, workspace_dir, payload)
            write_workflow_html_report(workspace_dir, payload)
            return payload
        try:
            write_workflow_si(workspace_dir, payload, raise_on_error=True)
        except Exception as exc:  # noqa: BLE001 - durable pending marker drives retry
            metadata["si_publish_error"] = f"{type(exc).__name__}: {exc}"[:500]
            permanent = isinstance(exc, (FileExistsError, ValueError))
            if permanent or attempts >= _SI_PUBLISH_MAX_ATTEMPTS:
                metadata["si_publish_pending"] = False
                metadata["si_publish_blocked"] = True
                metadata.pop("si_publish_next_retry_at", None)
                logger.warning("Workflow SI publication is blocked for %s", workspace_dir)
            else:
                delay_seconds = min(
                    _SI_PUBLISH_RETRY_MAX_SECONDS,
                    _SI_PUBLISH_RETRY_BASE_SECONDS * (2 ** (attempts - 1)),
                )
                if now is not None:
                    metadata["si_publish_next_retry_at"] = (
                        now + timedelta(seconds=delay_seconds)
                    ).isoformat()
                logger.warning("Workflow SI publication remains pending for %s", workspace_dir)
        else:
            metadata["si_publish_pending"] = False
            metadata["si_published_generation"] = generation
            metadata.pop("si_publish_error", None)
            metadata.pop("si_publish_blocked", None)
            metadata.pop("si_publish_attempts", None)
            metadata.pop("si_publish_next_retry_at", None)
        # Commit publication reconciliation independently from engine state. If
        # this write is ambiguous, the first checkpoint still says pending and a
        # terminal worker cycle retries the idempotent publication.
        o.persistence.write_workflow_payload(workspace_dir, payload)
        o.persistence.sync_workflow_registry(workflow_root_path, workspace_dir, payload)
        write_workflow_html_report(workspace_dir, payload)
        return payload


__all__ = [
    "advance_workflow",
    "cancel_materialized_workflow",
]
