from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.paths.workflow import validate_workflow_workspace_identity
from orca_auto.core.statuses import (
    STATUS_CANCEL_FAILED,
    STATUS_CANCEL_REQUESTED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    is_cancel_ack_status,
    is_stage_cancellable_status,
    is_stage_terminal_status,
)
from orca_auto.core.utils import normalize_text, timestamped_token
from orca_auto.flow.engine_options import WorkflowEngineOptions
from orca_auto.flow.orchestration.services import (
    OrchestrationServices,
    resolve_orchestration_services,
)
from orca_auto.flow.orchestration.stage_views import WorkflowPayloadView, WorkflowStageView
from orca_auto.flow.orchestration.support import submission_target_impl

_CancelTargetHandler = Callable[
    [OrchestrationServices, str, WorkflowEngineOptions],
    dict[str, Any],
]


@dataclass(frozen=True)
class _StageCancelOutcome:
    status: str
    reason: str = ""
    mode: str = ""

    @classmethod
    def skipped(cls, reason: str) -> _StageCancelOutcome:
        return cls(status="skipped", reason=reason)

    @classmethod
    def failed(cls, reason: str) -> _StageCancelOutcome:
        return cls(status=STATUS_FAILED, reason=reason)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> _StageCancelOutcome:
        return cls(
            status=str(payload.get("status", "") or ""),
            reason=str(payload.get("reason", "") or ""),
            mode=str(payload.get("mode", "") or ""),
        )

    @property
    def acknowledged(self) -> bool:
        return is_cancel_ack_status(self.status)

    def to_payload(self) -> dict[str, Any]:
        payload = {"status": self.status}
        if self.reason:
            payload["reason"] = self.reason
        if self.mode:
            payload["mode"] = self.mode
        return payload

    def cancelled_record(self, stage_id: str) -> dict[str, Any]:
        if self.mode:
            return {"stage_id": stage_id, "mode": self.mode}
        return {"stage_id": stage_id, "status": self.status}

    def failed_record(self, stage_id: str) -> dict[str, Any]:
        return {"stage_id": stage_id, "reason": self.reason or STATUS_CANCEL_FAILED}


def _missing_engine_config_cancel_result() -> dict[str, Any]:
    return {"status": STATUS_FAILED, "reason": "missing_engine_config"}


def _cancel_crest_target(
    services: OrchestrationServices,
    target: str,
    config: WorkflowEngineOptions,
) -> dict[str, Any]:
    config_path = normalize_text(config.crest.config)
    if not config_path:
        return _missing_engine_config_cancel_result()
    return services.engines.crest_cancel_target(target=target, config_path=config_path)


def _cancel_xtb_target(
    services: OrchestrationServices,
    target: str,
    config: WorkflowEngineOptions,
) -> dict[str, Any]:
    config_path = normalize_text(config.xtb.config)
    if not config_path:
        return _missing_engine_config_cancel_result()
    return services.engines.xtb_cancel_target(target=target, config_path=config_path)


def _cancel_orca_target(
    services: OrchestrationServices,
    target: str,
    config: WorkflowEngineOptions,
) -> dict[str, Any]:
    config_path = normalize_text(config.orca.config)
    if not config_path:
        return _missing_engine_config_cancel_result()
    return services.engines.orca_cancel_target(
        target=target,
        config_path=config_path,
        repo_root=config.orca.repo_root,
    )


_CANCEL_TARGET_HANDLERS: dict[str, _CancelTargetHandler] = {
    "crest": _cancel_crest_target,
    "xtb": _cancel_xtb_target,
    "orca": _cancel_orca_target,
}

_CANCELLATION_TRANSITIONS_KEY = "cancellation_status_transitions"
_CANCELLATION_EVENT_STATUSES = frozenset(
    {STATUS_CANCEL_REQUESTED, STATUS_CANCELLED, STATUS_CANCEL_FAILED, STATUS_FAILED}
)


def _record_cancellation_status_transition(
    payload: dict[str, Any],
    *,
    previous_status: str,
    status: str,
    occurred_at: str,
) -> list[dict[str, str]]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        payload["metadata"] = metadata
    raw_transitions = metadata.get(_CANCELLATION_TRANSITIONS_KEY)
    transitions: list[dict[str, str]] = []
    if isinstance(raw_transitions, list):
        for raw in raw_transitions[:8]:
            if not isinstance(raw, dict):
                continue
            raw_previous = normalize_text(raw.get("previous_status")).lower()
            raw_status = normalize_text(raw.get("status")).lower()
            event_id = normalize_text(raw.get("event_id"))
            event_occurred_at = normalize_text(raw.get("occurred_at"))
            if (
                not raw_previous
                or raw_previous == raw_status
                or raw_status not in _CANCELLATION_EVENT_STATUSES
                or not event_id
                or not event_occurred_at
            ):
                continue
            transition = {
                "event_id": event_id,
                "occurred_at": event_occurred_at,
                "previous_status": raw_previous,
                "status": raw_status,
            }
            if transition not in transitions:
                transitions.append(transition)
    candidate = {
        "event_id": timestamped_token("wf_evt"),
        "occurred_at": occurred_at,
        "previous_status": previous_status,
        "status": status,
    }
    if (
        previous_status
        and previous_status != status
        and status in _CANCELLATION_EVENT_STATUSES
        and candidate not in transitions
    ):
        transitions.append(candidate)
    metadata[_CANCELLATION_TRANSITIONS_KEY] = transitions
    return transitions


def _cancel_engine_target(
    *,
    services: OrchestrationServices,
    engine: str,
    target: str,
    config: WorkflowEngineOptions,
) -> dict[str, Any]:
    handler = _CANCEL_TARGET_HANDLERS.get(engine)
    if handler is None:
        return _missing_engine_config_cancel_result()
    return handler(services, target, config)


def _cancel_stage_activity(
    stage: dict[str, Any],
    *,
    config: WorkflowEngineOptions,
    services: OrchestrationServices | None = None,
) -> dict[str, Any]:
    return _cancel_stage_activity_outcome(
        stage,
        config=config,
        services=services,
    ).to_payload()


def _cancel_stage_activity_outcome(
    stage: dict[str, Any],
    *,
    config: WorkflowEngineOptions,
    services: OrchestrationServices | None = None,
) -> _StageCancelOutcome:
    resolved = resolve_orchestration_services(services)
    stage_view = WorkflowStageView.from_raw(stage)
    if stage_view is None or not stage_view.has_task:
        return _StageCancelOutcome.skipped("missing_task")

    status = stage_view.status_pair()
    if status.any_status(STATUS_CANCEL_REQUESTED):
        return _StageCancelOutcome.skipped("cancel_requested")
    if status.any_matches(is_stage_terminal_status):
        return _StageCancelOutcome.skipped("terminal")
    if not status.any_matches(is_stage_cancellable_status):
        return _StageCancelOutcome.skipped("not_cancellable")

    engine = stage_view.task_engine()
    cancel_target = submission_target_impl(stage)
    if not cancel_target:
        stage_view.set_status_pair(stage_status=STATUS_CANCELLED, task_status=STATUS_CANCELLED)
        return _StageCancelOutcome(status=STATUS_CANCELLED, mode="local")

    result = _cancel_engine_target(
        services=resolved,
        engine=engine,
        target=cancel_target,
        config=config,
    )

    stage_view.task.set_cancel_result(result)
    if is_cancel_ack_status(result.get("status")):
        stage_view.set_status_pair(stage_status=result["status"], task_status=result["status"])
        return _StageCancelOutcome(status=result["status"])
    return _StageCancelOutcome.failed(normalize_text(result.get("reason")) or STATUS_CANCEL_FAILED)


def _cancel_active_workflow_stages(
    payload: dict[str, Any],
    *,
    config: WorkflowEngineOptions,
    services: OrchestrationServices | None = None,
) -> dict[str, list[dict[str, Any]]]:
    resolved = resolve_orchestration_services(services)
    cancelled: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for stage_view in WorkflowPayloadView(payload).stage_views:
        outcome = _StageCancelOutcome.from_payload(
            _cancel_stage_activity(stage_view.raw, config=config, services=resolved)
        )
        stage_id = stage_view.stage_id()
        if outcome.acknowledged:
            cancelled.append(outcome.cancelled_record(stage_id))
            continue
        if outcome.status == STATUS_FAILED:
            failed.append(outcome.failed_record(stage_id))

    return {
        "cancelled": cancelled,
        "failed": failed,
    }


def cancel_materialized_workflow(
    *,
    target: str,
    workflow_root: str | Path,
    crest_config: str | None = None,
    xtb_config: str | None = None,
    orca_config: str | None = None,
    orca_repo_root: str | None = None,
    engine_options: WorkflowEngineOptions | None = None,
    services: OrchestrationServices | None = None,
) -> dict[str, Any]:
    resolved = resolve_orchestration_services(services)
    workflow_root_path = Path(workflow_root).expanduser().resolve()
    workspace_dir = resolved.persistence.resolve_workflow_workspace(
        target=target,
        workflow_root=workflow_root_path,
    )
    try:
        lock_context = resolved.persistence.acquire_workflow_lock(
            workspace_dir,
            timeout_seconds=5.0,
        )
        with lock_context:
            payload = resolved.persistence.load_workflow_payload(workspace_dir)
            # The caller-owned journal append below must read the whole journal
            # to dedupe its stable event id, and that read refuses oversized
            # files. Check capacity before cancelling stages or writing any
            # durable state, so an oversized journal refuses the cancel while
            # nothing has changed yet.
            resolved.events.require_workflow_journal_capacity(workflow_root_path)
            previous_status = str(payload.get("status") or "").strip().lower()
            identity_error = ""
            try:
                validate_workflow_workspace_identity(
                    workspace_dir,
                    payload.get("workflow_id"),
                )
            except ValueError as exc:
                identity_error = str(exc)
                metadata = payload.get("metadata")
                if not isinstance(metadata, dict):
                    metadata = {}
                    payload["metadata"] = metadata
                metadata["workflow_error"] = {
                    "status": STATUS_FAILED,
                    "scope": "workflow_identity_validation",
                    "reason": identity_error,
                    "message": identity_error,
                    "detected_at": resolved.clock.now_utc_iso(),
                }
            else:
                metadata = payload.get("metadata")
                workflow_error = (
                    metadata.get("workflow_error") if isinstance(metadata, dict) else None
                )
                if (
                    isinstance(metadata, dict)
                    and isinstance(workflow_error, dict)
                    and workflow_error.get("scope") == "workflow_identity_validation"
                ):
                    metadata.pop("workflow_error", None)
            config = engine_options or WorkflowEngineOptions.from_values(
                crest_config=crest_config,
                xtb_config=xtb_config,
                orca_config=orca_config,
                orca_repo_root=orca_repo_root,
            )
            cancellation = _cancel_active_workflow_stages(
                payload,
                config=config,
                services=resolved,
            )
            cancelled = cancellation["cancelled"]
            failed = cancellation["failed"]

            payload_view = WorkflowPayloadView(payload)
            pending_child_cancellation = any(
                stage_view.status_pair().any_status(STATUS_CANCEL_REQUESTED)
                for stage_view in payload_view.stage_views
            )
            if identity_error:
                payload_view.set_status(STATUS_FAILED)
            else:
                payload_view.set_status(
                    STATUS_CANCEL_REQUESTED
                    if pending_child_cancellation
                    else STATUS_CANCEL_FAILED
                    if failed
                    else STATUS_CANCELLED
                )
            current_status = str(payload_view.raw.get("status") or "").strip().lower()
            event_workflow_id = (
                workspace_dir.name
                if identity_error
                else str(payload.get("workflow_id") or workspace_dir.name)
            )
            pending_transitions = _record_cancellation_status_transition(
                payload,
                previous_status=previous_status,
                status=current_status,
                occurred_at=resolved.clock.now_utc_iso(),
            )
            resolved.persistence.write_workflow_payload(workspace_dir, payload)
            resolved.persistence.sync_workflow_registry(
                workflow_root_path,
                workspace_dir,
                payload,
            )
            for transition in list(pending_transitions):
                resolved.events.append_workflow_journal_event(
                    workflow_root_path,
                    event_id=transition["event_id"],
                    occurred_at=transition["occurred_at"],
                    event_type="workflow_status_changed",
                    workflow_id=event_workflow_id,
                    template_name=str(payload.get("template_name") or ""),
                    previous_status=transition["previous_status"],
                    status=transition["status"],
                    reason="cancel_requested",
                    worker_session_id="workflow_cancel",
                )
                pending_transitions.remove(transition)
            resolved.persistence.write_workflow_payload(workspace_dir, payload)
            resolved.persistence.sync_workflow_registry(
                workflow_root_path,
                workspace_dir,
                payload,
            )
            return {
                "workflow_id": event_workflow_id,
                "workspace_dir": str(workspace_dir),
                "status": payload_view.raw.get("status", ""),
                "cancelled": cancelled,
                "failed": failed,
            }
    except TimeoutError as exc:
        raise ValueError(
            f"Workflow is busy and could not be locked for cancellation within 5s: {workspace_dir}"
        ) from exc


__all__ = [
    "_StageCancelOutcome",
    "_cancel_active_workflow_stages",
    "_cancel_engine_target",
    "_cancel_stage_activity",
    "_cancel_stage_activity_outcome",
    "cancel_materialized_workflow",
]
