from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import MAX_RUN_ARTIFACT_JSON_BYTES
from orca_auto.core.engine_process import read_confined_text
from orca_auto.core.machine_observation import MACHINE_OBSERVATION_FILE
from orca_auto.core.paths.workflow import validate_workflow_workspace_identity
from orca_auto.core.utils import normalize_text
from orca_auto.flow.contracts.workflow import workflow_metadata
from orca_auto.flow.engine_options import WorkflowEngineOptions
from orca_auto.flow.orchestration.advance_phases import (
    AdvanceContext as _AdvanceContext,
)
from orca_auto.flow.orchestration.advance_phases import (
    _advance_phases,
    _finalize_advanced_workflow,
    _run_advance_phase,
)
from orca_auto.flow.orchestration.lifecycle import workflow_sync_only_impl
from orca_auto.flow.orchestration.services import (
    OrchestrationServices,
    resolve_orchestration_services,
)
from orca_auto.flow.orchestration.workflow_cancellation import (
    cancel_materialized_workflow,
)
from orca_auto.flow.workflow.machine import write_workflow_machine_observation
from orca_auto.flow.workflow.report import write_workflow_html_report
from orca_auto.flow.workflow.si.publication import write_workflow_si

logger = logging.getLogger(__name__)

_SI_PUBLISH_MAX_ATTEMPTS = 5


def _terminal_observation_published(workspace_dir: Path) -> bool:
    """True when this workspace already published its immutable ``machine.json``.

    The terminal observation pins the exact bytes of ``workflow_report.html``
    and ``workflow_si.md``; once it exists, no advance may regenerate a pinned
    artifact. An unsafe or unparseable existing observation fails closed with
    an error — before any pinned artifact could be rewritten — instead of
    silently freezing report publication behind a junk file.
    """
    machine_path = workspace_dir / MACHINE_OBSERVATION_FILE
    if machine_path.is_symlink():
        raise ValueError(f"workflow machine observation is unsafe: {machine_path}")
    if not machine_path.exists():
        return False
    try:
        payload = json.loads(
            read_confined_text(
                workspace_dir,
                machine_path,
                label="workflow machine observation",
                max_bytes=MAX_RUN_ARTIFACT_JSON_BYTES,
            )
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError(
            f"existing workflow machine observation is invalid: {machine_path}"
        ) from exc
    lifecycle = payload.get("lifecycle") if isinstance(payload, dict) else None
    phase = lifecycle.get("phase") if isinstance(lifecycle, dict) else None
    if phase != "finished":
        raise ValueError(f"existing workflow machine observation is not terminal: {machine_path}")
    return True


def _validate_or_quarantine_workflow_identity(
    payload: dict[str, Any],
    *,
    workspace_dir: Path,
    workflow_root_path: Path,
    services: OrchestrationServices,
) -> str:
    metadata = workflow_metadata(payload)
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
            "detected_at": services.clock.now_utc_iso(),
        }
        services.persistence.write_workflow_payload(workspace_dir, payload)
        services.persistence.sync_workflow_registry(workflow_root_path, workspace_dir, payload)
        try:
            observation_published = _terminal_observation_published(workspace_dir)
        except (OSError, RuntimeError, ValueError):
            # Quarantine must still persist; with the observation state unsafe
            # or unreadable, leave every possibly-pinned artifact untouched.
            logger.warning(
                "Workflow machine observation is unreadable during quarantine for %s",
                workspace_dir,
                exc_info=True,
            )
            observation_published = True
        if not observation_published:
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
    services: OrchestrationServices | None = None,
) -> dict[str, Any]:
    resolved = resolve_orchestration_services(services)
    workflow_root_path = Path(workflow_root).expanduser().resolve()
    config = engine_options or WorkflowEngineOptions.from_values(
        crest_config=crest_config,
        xtb_config=xtb_config,
        orca_config=orca_config,
        orca_repo_root=orca_repo_root,
    )
    workspace_dir = resolved.persistence.resolve_workflow_workspace(
        target=target,
        workflow_root=workflow_root_path,
    )
    with resolved.persistence.acquire_workflow_lock(workspace_dir):
        payload = resolved.persistence.load_workflow_payload(workspace_dir)
        workflow_id = _validate_or_quarantine_workflow_identity(
            payload,
            workspace_dir=workspace_dir,
            workflow_root_path=workflow_root_path,
            services=resolved,
        )
        sync_only = workflow_sync_only_impl(payload)
        context = _AdvanceContext(
            services=resolved,
            workflow_root_path=workflow_root_path,
            workspace_dir=workspace_dir,
            workflow_id=workflow_id,
            template_name=normalize_text(payload.get("template_name")),
            sync_only=sync_only,
            submit_ready=bool(submit_ready) and not sync_only,
        )
        for phase in _advance_phases(config):
            _run_advance_phase(payload, context, phase)

        _finalize_advanced_workflow(payload, context, config)
        if _terminal_observation_published(workspace_dir):
            # Re-advance of a published terminal workflow (crash/marker
            # reconciliation, manual CLI re-run) checkpoints private durable
            # state only; regenerating the HTML report here would invalidate
            # the immutable observation's receipt.
            resolved.persistence.write_workflow_payload(workspace_dir, payload)
            resolved.persistence.sync_workflow_registry(
                workflow_root_path,
                workspace_dir,
                payload,
            )
            return payload
        metadata = workflow_metadata(payload)
        was_blocked = bool(metadata.get("si_publish_blocked"))
        try:
            previous_attempts = max(0, int(metadata.get("si_publish_attempts", 0)))
        except (TypeError, ValueError):
            previous_attempts = 0
        if was_blocked:
            # Engine reconciliation may continue, but a blocked SI publication
            # is not retried by ordinary worker cycles.
            resolved.persistence.write_workflow_payload(workspace_dir, payload)
            resolved.persistence.sync_workflow_registry(
                workflow_root_path,
                workspace_dir,
                payload,
            )
            write_workflow_html_report(workspace_dir, payload)
            write_workflow_machine_observation(workspace_dir, payload)
            return payload
        attempts = previous_attempts + 1
        generation = str(metadata.get("last_advanced_at") or "").strip()
        metadata["si_publish_pending"] = True
        metadata["si_publish_generation"] = generation
        resolved.persistence.write_workflow_payload(workspace_dir, payload)
        resolved.persistence.sync_workflow_registry(workflow_root_path, workspace_dir, payload)
        write_workflow_html_report(workspace_dir, payload)
        # Count the SI writer attempt in authoritative workflow.json only after
        # registry discoverability is checkpointed. Registry lag/failure must
        # not consume the writer's crash budget.
        metadata["si_publish_attempts"] = attempts
        resolved.persistence.write_workflow_payload(workspace_dir, payload)
        if attempts > _SI_PUBLISH_MAX_ATTEMPTS:
            metadata["si_publish_pending"] = False
            metadata["si_publish_blocked"] = True
            metadata["si_publish_error"] = "SI publication attempt budget exhausted"
            resolved.persistence.write_workflow_payload(workspace_dir, payload)
            resolved.persistence.sync_workflow_registry(
                workflow_root_path,
                workspace_dir,
                payload,
            )
            write_workflow_html_report(workspace_dir, payload)
            write_workflow_machine_observation(workspace_dir, payload)
            return payload
        try:
            write_workflow_si(workspace_dir, payload)
        except Exception as exc:  # noqa: BLE001 - durable pending marker drives retry
            metadata["si_publish_error"] = f"{type(exc).__name__}: {exc}"[:500]
            permanent = isinstance(exc, (FileExistsError, ValueError))
            if permanent or attempts >= _SI_PUBLISH_MAX_ATTEMPTS:
                metadata["si_publish_pending"] = False
                metadata["si_publish_blocked"] = True
                logger.warning("Workflow SI publication is blocked for %s", workspace_dir)
            else:
                logger.warning("Workflow SI publication remains pending for %s", workspace_dir)
        else:
            metadata["si_publish_pending"] = False
            metadata["si_published_generation"] = generation
            metadata.pop("si_publish_error", None)
            metadata.pop("si_publish_blocked", None)
            metadata.pop("si_publish_attempts", None)
        # Commit publication reconciliation independently from engine state. If
        # this write is ambiguous, the first checkpoint still says pending and a
        # terminal worker cycle retries the idempotent publication.
        resolved.persistence.write_workflow_payload(workspace_dir, payload)
        resolved.persistence.sync_workflow_registry(workflow_root_path, workspace_dir, payload)
        write_workflow_html_report(workspace_dir, payload)
        write_workflow_machine_observation(workspace_dir, payload)
        return payload


__all__ = [
    "advance_workflow",
    "cancel_materialized_workflow",
]
