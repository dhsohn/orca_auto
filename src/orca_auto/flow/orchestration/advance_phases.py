from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.statuses import (
    STATUS_CANCEL_FAILED,
    STATUS_CANCEL_REQUESTED,
    STATUS_FAILED,
    is_stage_terminal_status,
)
from orca_auto.flow.contracts.workflow import workflow_stage_dicts
from orca_auto.flow.engine_options import WorkflowEngineOptions
from orca_auto.flow.orchestration.dep_types import OrchestrationDeps
from orca_auto.flow.orchestration.deps import (
    orchestration_context as _orchestration_context,
)
from orca_auto.flow.orchestration.stage_views import WorkflowPayloadView
from orca_auto.flow.workflow._phases import phase_finished


@dataclass(frozen=True)
class AdvanceContext:
    deps: OrchestrationDeps
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
    context.deps.stages.workflow._persist_workflow_progress(
        context.workflow_root_path,
        context.workspace_dir,
        payload,
        sync_only=context.sync_only,
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
    runtime = context.deps.stages.runtime
    for stage in workflow_stage_dicts(payload):
        runtime._sync_crest_stage(
            stage,
            crest_config=config.crest_config,
            submit_ready=context.submit_ready,
            workflow_id=context.workflow_id,
            workspace_dir=context.workspace_dir,
        )


def _append_reaction_xtb_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    if context.sync_only or context.template_name != "reaction_ts_search":
        return
    context.deps.stages.materialization._append_reaction_xtb_stages(
        payload,
        workspace_dir=context.workspace_dir,
        crest_config=config.crest_config,
    )


def _notify_crest_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    if context.sync_only:
        return
    context.deps.stages.workflow._maybe_notify_workflow_phase_summary(
        payload,
        config_path=config.crest_config,
        phase_engine="crest",
    )


def _sync_xtb_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    runtime = context.deps.stages.runtime
    for stage in workflow_stage_dicts(payload):
        runtime._sync_xtb_stage(
            stage,
            xtb_config=config.xtb_config,
            submit_ready=context.submit_ready,
            workflow_id=context.workflow_id,
            workspace_dir=context.workspace_dir,
        )


def _clear_xtb_handoff_phase(
    payload: dict[str, Any], context: AdvanceContext, _config: WorkflowEngineOptions
) -> None:
    context.deps.stages.support._clear_reaction_xtb_handoff_error_if_recovering(payload)


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
    context.deps.stages.materialization._append_reaction_orca_stages(
        payload,
        workspace_dir=context.workspace_dir,
        xtb_config=config.xtb_config,
        orca_config=config.orca_config,
    )


def _orca_stage_count(payload: dict[str, Any], *, deps: OrchestrationDeps | None = None) -> int:
    o = _orchestration_context(deps)
    return sum(
        1
        for stage_view in WorkflowPayloadView(payload).stage_views
        if stage_view.task_engine(o) == "orca"
    )


def _notify_xtb_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    if not _reaction_orca_ready(payload, context):
        return
    context.deps.stages.workflow._maybe_notify_workflow_phase_summary(
        payload,
        config_path=config.xtb_config,
        phase_engine="xtb",
        extra_lines=[f"planned_orca_stages: {_orca_stage_count(payload, deps=context.deps)}"],
    )


def _append_conformer_orca_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    if context.sync_only or context.template_name != "conformer_screening":
        return
    context.deps.stages.materialization._append_crest_orca_stages(
        payload,
        template_name="conformer_screening",
        crest_config=config.crest_config,
        orca_config=config.orca_config,
        stage_id_prefix="orca_conformer",
        xyz_filename="conformer_guess.xyz",
        inp_filename="conformer_opt.inp",
    )


def _sync_orca_phase(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    runtime = context.deps.stages.runtime
    for stage in workflow_stage_dicts(payload):
        runtime._sync_orca_stage(
            stage,
            orca_config=config.orca_config,
            orca_repo_root=config.orca_repo_root,
            submit_ready=context.submit_ready,
        )


def _append_scan_optts_phase(
    payload: dict[str, Any], context: AdvanceContext, _config: WorkflowEngineOptions
) -> None:
    """Fan out OptTS candidates once the scan_ts_search relaxed scan completed.

    Runs after the ORCA sync so it sees the scan stage's fresh terminal status;
    the appended stages are submitted by the next advance cycle's sync.
    """
    if context.sync_only or context.template_name != "scan_ts_search":
        return
    context.deps.stages.materialization._append_scan_optts_stages(
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
        bind(_append_scan_optts_phase),
    )


def _finalize_advanced_workflow(
    payload: dict[str, Any], context: AdvanceContext, config: WorkflowEngineOptions
) -> None:
    o = context.deps
    payload_view = WorkflowPayloadView(payload)
    payload_view.set_status(o.stages.workflow._recompute_workflow_status(payload))
    if payload_view.status(o.stages.support._normalize_text) == STATUS_FAILED:
        o.advance._cancel_active_workflow_stages(payload, config=config)
        payload_view.set_status(o.stages.workflow._recompute_workflow_status(payload))

    metadata = payload_view.metadata()
    if metadata is None:
        return
    metadata["last_advanced_at"] = o.persistence.now_utc_iso()
    metadata["sync_only"] = bool(context.sync_only)
    payload_status = payload_view.status(o.stages.support._normalize_text)
    final_child_sync_pending = (
        is_stage_terminal_status(payload_status)
        or payload_status in {STATUS_CANCEL_REQUESTED, STATUS_CANCEL_FAILED}
    ) and o.stages.workflow._workflow_has_active_children(payload)
    metadata["final_child_sync_pending"] = final_child_sync_pending
    if final_child_sync_pending:
        metadata["final_child_sync_completed_at"] = ""
    else:
        metadata["final_child_sync_completed_at"] = o.persistence.now_utc_iso()


__all__ = [
    "AdvanceContext",
    "AdvancePhase",
    "ConfiguredAdvancePhase",
    "_advance_phases",
    "_append_conformer_orca_phase",
    "_append_reaction_orca_phase",
    "_append_reaction_xtb_phase",
    "_append_scan_optts_phase",
    "_checkpoint_advance_phase",
    "_clear_xtb_handoff_phase",
    "_finalize_advanced_workflow",
    "_notify_crest_phase",
    "_notify_xtb_phase",
    "_orca_stage_count",
    "_reaction_orca_ready",
    "_run_advance_phase",
    "_sync_crest_phase",
    "_sync_orca_phase",
    "_sync_xtb_phase",
]
