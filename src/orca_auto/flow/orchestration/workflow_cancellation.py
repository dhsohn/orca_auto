from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.statuses import (
    STATUS_CANCEL_FAILED,
    STATUS_CANCEL_REQUESTED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    is_cancel_ack_status,
    is_stage_cancellable_status,
    is_stage_terminal_status,
)
from orca_auto.flow.engine_options import WorkflowEngineOptions
from orca_auto.flow.orchestration.dep_types import OrchestrationDeps
from orca_auto.flow.orchestration.deps import (
    orchestration_context as _orchestration_context,
)
from orca_auto.flow.orchestration.stage_views import WorkflowPayloadView, WorkflowStageView

_CancelTargetHandler = Callable[
    [OrchestrationDeps, str, WorkflowEngineOptions],
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
    deps: OrchestrationDeps,
    target: str,
    config: WorkflowEngineOptions,
) -> dict[str, Any]:
    config_path = deps.stages.support._normalize_text(config.crest.config)
    if not config_path:
        return _missing_engine_config_cancel_result()
    return deps.engines.crest_cancel_target(target=target, config_path=config_path)


def _cancel_xtb_target(
    deps: OrchestrationDeps,
    target: str,
    config: WorkflowEngineOptions,
) -> dict[str, Any]:
    config_path = deps.stages.support._normalize_text(config.xtb.config)
    if not config_path:
        return _missing_engine_config_cancel_result()
    return deps.engines.xtb_cancel_target(target=target, config_path=config_path)


def _cancel_orca_target(
    deps: OrchestrationDeps,
    target: str,
    config: WorkflowEngineOptions,
) -> dict[str, Any]:
    config_path = deps.stages.support._normalize_text(config.orca.config)
    if not config_path:
        return _missing_engine_config_cancel_result()
    return deps.engines.orca_cancel_target(
        target=target,
        config_path=config_path,
        repo_root=config.orca.repo_root,
    )


_CANCEL_TARGET_HANDLERS: dict[str, _CancelTargetHandler] = {
    "crest": _cancel_crest_target,
    "xtb": _cancel_xtb_target,
    "orca": _cancel_orca_target,
}


def _cancel_engine_target(
    *,
    deps: OrchestrationDeps,
    engine: str,
    target: str,
    config: WorkflowEngineOptions,
) -> dict[str, Any]:
    handler = _CANCEL_TARGET_HANDLERS.get(engine)
    if handler is None:
        return _missing_engine_config_cancel_result()
    return handler(deps, target, config)


def _cancel_stage_activity(
    stage: dict[str, Any],
    *,
    config: WorkflowEngineOptions,
    deps: OrchestrationDeps | None = None,
) -> dict[str, Any]:
    return _cancel_stage_activity_outcome(stage, config=config, deps=deps).to_payload()


def _cancel_stage_activity_outcome(
    stage: dict[str, Any],
    *,
    config: WorkflowEngineOptions,
    deps: OrchestrationDeps | None = None,
) -> _StageCancelOutcome:
    o = _orchestration_context(deps)
    stage_view = WorkflowStageView.from_raw(stage)
    if stage_view is None or not stage_view.has_task:
        return _StageCancelOutcome.skipped("missing_task")

    status = stage_view.status_pair(o)
    if status.any_status(STATUS_CANCEL_REQUESTED):
        return _StageCancelOutcome.skipped("cancel_requested")
    if status.any_matches(is_stage_terminal_status):
        return _StageCancelOutcome.skipped("terminal")
    if not status.any_matches(is_stage_cancellable_status):
        return _StageCancelOutcome.skipped("not_cancellable")

    engine = stage_view.task_engine(o)
    cancel_target = o.stages.support._submission_target(stage)
    if not cancel_target:
        stage_view.set_status_pair(stage_status=STATUS_CANCELLED, task_status=STATUS_CANCELLED)
        return _StageCancelOutcome(status=STATUS_CANCELLED, mode="local")

    result = _cancel_engine_target(
        deps=o,
        engine=engine,
        target=cancel_target,
        config=config,
    )

    stage_view.task.set_cancel_result(result)
    if is_cancel_ack_status(result.get("status")):
        stage_view.set_status_pair(stage_status=result["status"], task_status=result["status"])
        return _StageCancelOutcome(status=result["status"])
    return _StageCancelOutcome.failed(
        o.stages.support._normalize_text(result.get("reason")) or STATUS_CANCEL_FAILED
    )


def _cancel_active_workflow_stages(
    payload: dict[str, Any],
    *,
    config: WorkflowEngineOptions,
    deps: OrchestrationDeps | None = None,
) -> dict[str, list[dict[str, Any]]]:
    o = _orchestration_context(deps)
    cancelled: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for stage_view in WorkflowPayloadView(payload).stage_views:
        outcome = _StageCancelOutcome.from_payload(
            o.advance._cancel_stage_activity(stage_view.raw, config=config)
        )
        stage_id = stage_view.stage_id(o)
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
    deps: OrchestrationDeps | None = None,
) -> dict[str, Any]:
    o = _orchestration_context(deps)
    workflow_root_path = Path(workflow_root).expanduser().resolve()
    workspace_dir = o.persistence.resolve_workflow_workspace(
        target=target,
        workflow_root=workflow_root_path,
    )
    try:
        lock_context = o.persistence.acquire_workflow_lock(workspace_dir, timeout_seconds=5.0)
        with lock_context:
            payload = o.persistence.load_workflow_payload(workspace_dir)
            config = engine_options or WorkflowEngineOptions.from_values(
                crest_config=crest_config,
                xtb_config=xtb_config,
                orca_config=orca_config,
                orca_repo_root=orca_repo_root,
            )
            cancellation = o.advance._cancel_active_workflow_stages(payload, config=config)
            cancelled = cancellation["cancelled"]
            failed = cancellation["failed"]

            payload_view = WorkflowPayloadView(payload)
            payload_view.set_status(
                STATUS_CANCEL_REQUESTED
                if any(item.get("status") == STATUS_CANCEL_REQUESTED for item in cancelled)
                else STATUS_CANCEL_FAILED
                if failed
                else STATUS_CANCELLED
            )
            o.persistence.write_workflow_payload(workspace_dir, payload)
            o.persistence.sync_workflow_registry(workflow_root_path, workspace_dir, payload)
            return {
                "workflow_id": payload.get("workflow_id", ""),
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
