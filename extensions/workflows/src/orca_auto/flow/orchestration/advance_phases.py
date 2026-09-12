from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.statuses import (
    STATUS_CANCEL_FAILED,
    STATUS_CANCEL_REQUESTED,
    WORKFLOW_FAILED_STATUSES,
    is_stage_terminal_status,
)
from orca_auto.core.utils import normalize_text
from orca_auto.flow.contracts.workflow import workflow_stage_dicts
from orca_auto.flow.engine_options import WorkflowEngineOptions
from orca_auto.flow.orchestration.crest_orca_materialization import (
    append_crest_orca_stages_impl,
)
from orca_auto.flow.orchestration.interaction_energy_materialization import (
    append_interaction_energy_stages_impl,
)
from orca_auto.flow.orchestration.lifecycle import (
    recompute_workflow_status_impl,
    workflow_has_active_children_impl,
)
from orca_auto.flow.orchestration.services import OrchestrationServices
from orca_auto.flow.orchestration.stage_runtime.crest import sync_crest_stage_impl
from orca_auto.flow.orchestration.stage_runtime.orca import sync_orca_stage_impl
from orca_auto.flow.orchestration.stage_views import WorkflowPayloadView
from orca_auto.flow.orchestration.workflow_cancellation import _cancel_active_workflow_stages


@dataclass(frozen=True)
class AdvanceContext:
    services: OrchestrationServices
    workflow_root_path: Path
    workspace_dir: Path
    workflow_id: str
    template_name: str
    sync_only: bool
    submit_ready: bool


AdvancePhase = Callable[[dict[str, Any], AdvanceContext], None]
ConfiguredAdvancePhase = Callable[
    [dict[str, Any], AdvanceContext, WorkflowEngineOptions],
    None,
]


def _checkpoint_advance_phase(
    payload: dict[str, Any],
    previous_payload: dict[str, Any],
    context: AdvanceContext,
) -> None:
    if payload == previous_payload:
        return
    if not context.sync_only:
        status = normalize_text(payload.get("status")).lower()
        if status not in {
            "completed",
            "failed",
            "cancel_requested",
            "cancelled",
            "cancel_failed",
        }:
            payload["status"] = "running"
    context.services.persistence.write_workflow_payload(context.workspace_dir, payload)
    context.services.persistence.sync_workflow_registry(
        context.workflow_root_path,
        context.workspace_dir,
        payload,
    )


def _run_advance_phase(
    payload: dict[str, Any],
    context: AdvanceContext,
    phase: AdvancePhase,
) -> None:
    before_phase = deepcopy(payload)
    phase(payload, context)
    _checkpoint_advance_phase(payload, before_phase, context)


def _sync_crest_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    for stage in workflow_stage_dicts(payload):
        sync_crest_stage_impl(
            stage,
            crest_config=config.crest_config,
            submit_ready=context.submit_ready,
            workflow_id=context.workflow_id,
            workspace_dir=context.workspace_dir,
            services=context.services,
        )


def _notify_crest_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    if context.sync_only:
        return
    context.services.events.notify_phase_summary(
        payload=payload,
        config_path=config.crest_config,
        phase_engine="crest",
    )


def _all_orca_stages_terminal(payload: dict[str, Any]) -> bool:
    stage_views = [
        stage_view
        for stage_view in WorkflowPayloadView(payload).stage_views
        if stage_view.task_engine() == "orca"
    ]
    return bool(stage_views) and all(
        is_stage_terminal_status(stage_view.status()) for stage_view in stage_views
    )


def _append_conformer_orca_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    if context.sync_only or context.template_name != "conformer_screening":
        return
    append_crest_orca_stages_impl(
        payload,
        template_name="conformer_screening",
        crest_config=config.crest_config,
        orca_config=config.orca_config,
        stage_id_prefix="orca_conformer",
        xyz_filename="conformer_guess.xyz",
        inp_filename="conformer_opt.inp",
        services=context.services,
    )


def _sync_orca_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    for stage in workflow_stage_dicts(payload):
        sync_orca_stage_impl(
            stage,
            orca_config=config.orca_config,
            submit_ready=context.submit_ready,
            services=context.services,
        )


def _record_orca_exhaustion_after_sync_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    if context.sync_only or not _all_orca_stages_terminal(payload):
        return
    _append_conformer_orca_phase(payload, context, config)


def _append_interaction_energy_phase(
    payload: dict[str, Any], context: AdvanceContext, _config: WorkflowEngineOptions
) -> None:
    """Fan out ΔE_int single points once the conformer optimizations are terminal.

    Runs after the ORCA sync so it sees fresh terminal statuses; the appended
    single points are submitted by the next advance cycle. The materializer gates
    on interaction_energy.enabled and is idempotent, so this is a safe no-op for
    every other workflow and on repeat advances.
    """
    if context.sync_only or context.template_name != "conformer_screening":
        return
    append_interaction_energy_stages_impl(
        payload,
        workspace_dir=context.workspace_dir,
    )


def _advance_phases(config: WorkflowEngineOptions) -> tuple[AdvancePhase, ...]:
    def bind(phase: ConfiguredAdvancePhase) -> AdvancePhase:
        return lambda payload, context: phase(payload, context, config)

    return (
        bind(_sync_crest_phase),
        bind(_notify_crest_phase),
        bind(_append_conformer_orca_phase),
        bind(_sync_orca_phase),
        bind(_record_orca_exhaustion_after_sync_phase),
        bind(_append_interaction_energy_phase),
    )


def _finalize_advanced_workflow(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    services = context.services
    payload_view = WorkflowPayloadView(payload)
    payload_view.set_status(_recompute_workflow_status(payload))
    if payload_view.status(normalize_text) in WORKFLOW_FAILED_STATUSES:
        _cancel_active_workflow_stages(payload, config=config, services=services)
        payload_view.set_status(_recompute_workflow_status(payload))

    metadata = payload_view.metadata()
    if metadata is None:
        return
    metadata["last_advanced_at"] = services.clock.now_utc_iso()
    metadata["sync_only"] = bool(context.sync_only)
    payload_status = payload_view.status(normalize_text)
    final_child_sync_pending = (
        is_stage_terminal_status(payload_status)
        or payload_status in {STATUS_CANCEL_REQUESTED, STATUS_CANCEL_FAILED}
    ) and workflow_has_active_children_impl(payload)
    metadata["final_child_sync_pending"] = final_child_sync_pending
    if final_child_sync_pending:
        metadata["final_child_sync_completed_at"] = ""
    else:
        metadata["final_child_sync_completed_at"] = services.clock.now_utc_iso()


def _recompute_workflow_status(payload: dict[str, Any]) -> str:
    return recompute_workflow_status_impl(payload)


__all__ = [
    "AdvanceContext",
    "AdvancePhase",
    "ConfiguredAdvancePhase",
    "_advance_phases",
    "_append_conformer_orca_phase",
    "_append_interaction_energy_phase",
    "_checkpoint_advance_phase",
    "_finalize_advanced_workflow",
    "_notify_crest_phase",
    "_record_orca_exhaustion_after_sync_phase",
    "_run_advance_phase",
    "_sync_crest_phase",
    "_sync_orca_phase",
]
