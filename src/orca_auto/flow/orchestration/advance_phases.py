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
from orca_auto.flow.contracts.workflow import workflow_stage_dicts, workflow_stage_metadata
from orca_auto.flow.engine_options import WorkflowEngineOptions
from orca_auto.flow.orchestration.crest_orca_materialization import (
    append_crest_orca_stages_impl,
)
from orca_auto.flow.orchestration.interaction_energy_materialization import (
    append_interaction_energy_stages_impl,
)
from orca_auto.flow.orchestration.lifecycle import (
    effective_stage_status_impl,
    recompute_workflow_status_impl,
    stage_failure_is_recoverable_impl,
    workflow_has_active_children_impl,
)
from orca_auto.flow.orchestration.reaction_materialization import append_reaction_xtb_stages_impl
from orca_auto.flow.orchestration.reaction_orca_materialization import (
    append_reaction_orca_stages_impl,
)
from orca_auto.flow.orchestration.scan_orca_materialization import append_scan_optts_stages_impl
from orca_auto.flow.orchestration.services import OrchestrationServices
from orca_auto.flow.orchestration.stage_runtime.crest import sync_crest_stage_impl
from orca_auto.flow.orchestration.stage_runtime.orca import sync_orca_stage_impl
from orca_auto.flow.orchestration.stage_runtime.xtb_sync import sync_xtb_stage_impl
from orca_auto.flow.orchestration.stage_views import WorkflowPayloadView
from orca_auto.flow.orchestration.support import clear_reaction_xtb_handoff_error_if_recovering_impl
from orca_auto.flow.orchestration.workflow_cancellation import _cancel_active_workflow_stages
from orca_auto.flow.state import workflow_has_active_downstream
from orca_auto.flow.workflow._phases import phase_finished


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


def _append_reaction_xtb_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    if context.sync_only or context.template_name != "reaction_ts_search":
        return
    append_reaction_xtb_stages_impl(
        payload,
        workspace_dir=context.workspace_dir,
        crest_config=config.crest_config,
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


def _sync_xtb_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    for stage in workflow_stage_dicts(payload):
        sync_xtb_stage_impl(
            stage,
            xtb_config=config.xtb_config,
            submit_ready=context.submit_ready,
            workflow_id=context.workflow_id,
            workspace_dir=context.workspace_dir,
            services=context.services,
        )


def _clear_xtb_handoff_phase(
    payload: dict[str, Any], context: AdvanceContext, _config: WorkflowEngineOptions
) -> None:
    clear_reaction_xtb_handoff_error_if_recovering_impl(payload)


def _reaction_orca_ready(payload: dict[str, Any], context: AdvanceContext) -> bool:
    return (
        not context.sync_only
        and context.template_name == "reaction_ts_search"
        and phase_finished(payload.get("stages", []), engine="xtb")
    )


def _append_reaction_orca_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    if not _reaction_orca_ready(payload, context):
        return
    append_reaction_orca_stages_impl(
        payload,
        workspace_dir=context.workspace_dir,
        xtb_config=config.xtb_config,
        orca_config=config.orca_config,
        services=context.services,
    )


def _orca_stage_count(payload: dict[str, Any]) -> int:
    return sum(
        1
        for stage_view in WorkflowPayloadView(payload).stage_views
        if stage_view.task_engine() == "orca"
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


def _notify_xtb_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    if not _reaction_orca_ready(payload, context):
        return
    context.services.events.notify_phase_summary(
        payload=payload,
        config_path=config.xtb_config,
        phase_engine="xtb",
        extra_lines=[f"planned_orca_stages: {_orca_stage_count(payload)}"],
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
    _append_reaction_orca_phase(payload, context, config)
    _append_conformer_orca_phase(payload, context, config)


def _append_scan_optts_phase(
    payload: dict[str, Any], context: AdvanceContext, _config: WorkflowEngineOptions
) -> None:
    """Fan out OptTS candidates once the scan_ts_search relaxed scan completed.

    Runs after the ORCA sync so it sees the scan stage's fresh terminal status;
    the appended stages are submitted by the next advance cycle's sync.
    """
    if context.sync_only or context.template_name != "scan_ts_search":
        return
    append_scan_optts_stages_impl(
        payload,
        workspace_dir=context.workspace_dir,
    )


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
        bind(_append_reaction_xtb_phase),
        bind(_notify_crest_phase),
        bind(_sync_xtb_phase),
        bind(_clear_xtb_handoff_phase),
        bind(_append_reaction_orca_phase),
        bind(_notify_xtb_phase),
        bind(_append_conformer_orca_phase),
        bind(_sync_orca_phase),
        bind(_record_orca_exhaustion_after_sync_phase),
        bind(_append_scan_optts_phase),
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
    ) and workflow_has_active_children_impl(
        payload,
        workflow_has_active_downstream_fn=workflow_has_active_downstream,
    )
    metadata["final_child_sync_pending"] = final_child_sync_pending
    if final_child_sync_pending:
        metadata["final_child_sync_completed_at"] = ""
    else:
        metadata["final_child_sync_completed_at"] = services.clock.now_utc_iso()


def _recompute_workflow_status(payload: dict[str, Any]) -> str:
    def stage_failure_is_recoverable(stage: dict[str, Any]) -> bool:
        return stage_failure_is_recoverable_impl(
            stage,
            stage_metadata_fn=workflow_stage_metadata,
        )

    return recompute_workflow_status_impl(
        payload,
        effective_stage_status_fn=lambda stage: effective_stage_status_impl(
            stage,
            stage_failure_is_recoverable_fn=stage_failure_is_recoverable,
        ),
    )


__all__ = [
    "AdvanceContext",
    "AdvancePhase",
    "ConfiguredAdvancePhase",
    "_advance_phases",
    "_append_conformer_orca_phase",
    "_append_interaction_energy_phase",
    "_append_reaction_orca_phase",
    "_append_reaction_xtb_phase",
    "_append_scan_optts_phase",
    "_checkpoint_advance_phase",
    "_clear_xtb_handoff_phase",
    "_finalize_advanced_workflow",
    "_notify_crest_phase",
    "_notify_xtb_phase",
    "_orca_stage_count",
    "_record_orca_exhaustion_after_sync_phase",
    "_reaction_orca_ready",
    "_run_advance_phase",
    "_sync_crest_phase",
    "_sync_orca_phase",
    "_sync_xtb_phase",
]
