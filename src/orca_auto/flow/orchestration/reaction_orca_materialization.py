from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.statuses import is_failed_status, is_queue_active_status
from orca_auto.core.utils import normalize_text
from orca_auto.flow.contracts import WorkflowStageInput
from orca_auto.flow.contracts.workflow import (
    workflow_request_parameters,
    workflow_stage_metadata,
    workflow_task_payload_dict,
)
from orca_auto.flow.orchestration.charge_spin import strict_int
from orca_auto.flow.orchestration.lifecycle import stage_failure_is_recoverable_impl
from orca_auto.flow.orchestration.scan_orca_materialization import (
    _all_terminal_none_verified,
    _record_workflow_error,
)
from orca_auto.flow.orchestration.services import (
    OrchestrationServices,
    resolve_orchestration_services,
)
from orca_auto.flow.orchestration.stage_views import (
    WorkflowStageView,
    _clear_workflow_error_scope,
    _engine_stage_views,
    _engine_stages,
    _stage_views,
)
from orca_auto.flow.orchestration.support import (
    load_config_root_impl,
    reaction_orca_source_candidate_path_impl,
    reaction_ts_guess_error_impl,
    required_stage_budget,
    select_valid_ts_guess_inputs,
    submission_target_impl,
)
from orca_auto.flow.state import workflow_workspace_internal_engine_paths
from orca_auto.flow.workflow._phases import phase_finished

if TYPE_CHECKING:
    from orca_auto.flow.contracts.xtb import XtbArtifactContract


@dataclass(frozen=True)
class _ReactionOrcaStagePlan:
    payload_metadata: dict[str, Any]
    params: dict[str, Any]
    orca_allowed_root: Path
    ordered_candidates: list[Any]
    remaining_candidates: list[Any]
    existing_stages: list[dict[str, Any]]
    max_stage_count: int
    omitted_candidate_count: int
    has_pending_xtb: bool
    handoff_errors: list[dict[str, str]]


def _completed_or_recoverable_xtb_stages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        view.raw
        for view in _engine_stage_views(payload, "xtb")
        if view.status() == "completed"
        or stage_failure_is_recoverable_impl(
            view.raw,
            normalize_text_fn=normalize_text,
            stage_metadata_fn=workflow_stage_metadata,
        )
    ]


def _load_xtb_contract_for_stage(
    services: OrchestrationServices,
    xtb_stage: dict[str, Any],
    *,
    xtb_allowed_root: Path,
) -> XtbArtifactContract | None:
    view = WorkflowStageView.from_raw(xtb_stage)
    if view is None or not view.task.raw:
        return None
    payload_dict = workflow_task_payload_dict(view.task.raw)
    target = normalize_text(payload_dict.get("job_dir")) or submission_target_impl(xtb_stage)
    if not target:
        return None
    try:
        return services.engines.load_xtb_artifact_contract(
            xtb_index_root=xtb_allowed_root,
            target=target,
        )
    except FileNotFoundError:
        return None


def _record_xtb_handoff_error(
    services: OrchestrationServices,
    xtb_stage: dict[str, Any],
    contract: XtbArtifactContract,
) -> dict[str, str]:
    stage_metadata = workflow_stage_metadata(xtb_stage)
    error = reaction_ts_guess_error_impl(contract, services=services)
    stage_metadata["reaction_handoff_status"] = "failed"
    stage_metadata["reaction_handoff_reason"] = error["reason"]
    stage_metadata["reaction_handoff_message"] = error["message"]
    return {
        "stage_id": WorkflowStageView(xtb_stage).stage_id(),
        "job_id": normalize_text(contract.job_id),
        "reason": error["reason"],
        "message": error["message"],
    }


def _mark_xtb_handoff_ready(xtb_stage: dict[str, Any]) -> None:
    stage_metadata = workflow_stage_metadata(xtb_stage)
    stage_metadata.pop("reaction_handoff_reason", None)
    stage_metadata.pop("reaction_handoff_message", None)
    stage_metadata["reaction_handoff_status"] = "ready"


def _reaction_orca_candidate_pool_rows(
    xtb_stage: dict[str, Any],
    contract: XtbArtifactContract,
    inputs: list[Any],
    stage_order: int,
) -> list[tuple[int, int, str, Any]]:
    rows: list[tuple[int, int, str, Any]] = []
    for candidate in inputs:
        candidate_path = normalize_text(candidate.artifact_path)
        if not candidate_path:
            continue
        candidate_metadata = {
            **dict(candidate.metadata),
            "xtb_stage_id": WorkflowStageView(xtb_stage).stage_id(),
            "xtb_stage_order": int(stage_order),
            "xtb_source_job_id": normalize_text(contract.job_id),
            "xtb_source_job_type": normalize_text(contract.job_type),
        }
        rows.append(
            (
                int(stage_order),
                candidate.rank if candidate.rank > 0 else 10_000,
                candidate_path,
                WorkflowStageInput(
                    source_job_id=candidate.source_job_id,
                    source_job_type=candidate.source_job_type,
                    reaction_key=candidate.reaction_key,
                    selected_input_xyz=candidate.selected_input_xyz,
                    rank=candidate.rank,
                    kind=candidate.kind,
                    artifact_path=candidate.artifact_path,
                    selected=candidate.selected,
                    score=candidate.score,
                    metadata=candidate_metadata,
                ),
            )
        )
    return rows


def _collect_reaction_orca_candidates(
    services: OrchestrationServices,
    xtb_stages: list[dict[str, Any]],
    *,
    xtb_allowed_root: Path,
) -> tuple[list[tuple[int, int, str, Any]], list[dict[str, str]]]:
    candidate_pool: list[tuple[int, int, str, Any]] = []
    handoff_errors: list[dict[str, str]] = []
    for xtb_stage_index, xtb_stage in enumerate(xtb_stages, start=1):
        contract = _load_xtb_contract_for_stage(
            services,
            xtb_stage,
            xtb_allowed_root=xtb_allowed_root,
        )
        if contract is None:
            continue
        valid_inputs = select_valid_ts_guess_inputs(services, contract)
        if not valid_inputs:
            handoff_errors.append(_record_xtb_handoff_error(services, xtb_stage, contract))
            continue
        _mark_xtb_handoff_ready(xtb_stage)
        candidate_pool.extend(
            _reaction_orca_candidate_pool_rows(xtb_stage, contract, valid_inputs, xtb_stage_index)
        )
    return candidate_pool, handoff_errors


def _unique_ordered_candidates(candidate_pool: list[tuple[int, int, str, Any]]) -> list[Any]:
    candidate_pool.sort(key=lambda item: (item[0], item[1], item[2]))
    ordered_candidates: list[Any] = []
    seen_candidate_paths: set[str] = set()
    for _, _, candidate_path, candidate in candidate_pool:
        if candidate_path in seen_candidate_paths:
            continue
        seen_candidate_paths.add(candidate_path)
        ordered_candidates.append(candidate)
    return ordered_candidates


def _has_pending_xtb_stage(payload: dict[str, Any]) -> bool:
    return any(
        view.task_engine() == "xtb"
        and (is_queue_active_status(view.status()) or is_queue_active_status(view.task_status()))
        for view in _stage_views(payload)
    )


def _remaining_orca_candidates(
    existing: list[dict[str, Any]], ordered_candidates: list[Any]
) -> list[Any]:
    attempted_paths = {
        reaction_orca_source_candidate_path_impl(stage)
        for stage in existing
        if reaction_orca_source_candidate_path_impl(stage)
    }
    return [
        candidate
        for candidate in ordered_candidates
        if normalize_text(candidate.artifact_path) not in attempted_paths
    ]


def _record_reaction_handoff_failure(
    payload_metadata: dict[str, Any],
    *,
    existing: list[dict[str, Any]],
    has_pending_xtb: bool,
    handoff_errors: list[dict[str, str]],
) -> None:
    if (
        not existing
        and not has_pending_xtb
        and handoff_errors
        and isinstance(payload_metadata, dict)
    ):
        payload_metadata["workflow_error"] = {
            "status": "failed",
            "scope": "reaction_ts_search_xtb_handoff",
            **handoff_errors[0],
        }


def _maybe_record_reaction_xtb_phase_failure(payload: dict[str, Any]) -> None:
    """Fail the workflow when the finished xTB phase yielded no usable stage.

    ``append_reaction_orca_stages_impl`` only runs once the xTB phase is finished.
    If no xTB stage is completed or recoverable -- so no ORCA candidate stage is
    ever materialized and no per-stage handoff error is collected -- but at least
    one xTB stage failed outright, the reaction TS search produced no TS guess and
    must be recorded as a workflow failure. Without this, every-stage-terminal is
    reported COMPLETED by recompute_workflow_status.
    """
    if _completed_or_recoverable_xtb_stages(payload):
        return
    views = _engine_stage_views(payload, "xtb")
    failed = next((view for view in views if is_failed_status(view.status())), None)
    if failed is None:
        return
    payload_metadata = payload.setdefault("metadata", {})
    if not isinstance(payload_metadata, dict) or isinstance(
        payload_metadata.get("workflow_error"), dict
    ):
        return
    workflow_error: dict[str, str] = {
        "status": "failed",
        "scope": "reaction_ts_search_xtb_handoff",
        "reason": "reaction_ts_search_xtb_phase_failed",
        "message": "All xTB path search stages failed; no TS guess was produced.",
    }
    stage_id = normalize_text(failed.raw.get("stage_id"))
    if stage_id:
        workflow_error["stage_id"] = stage_id
    payload_metadata["workflow_error"] = workflow_error


def _maybe_record_reaction_orca_candidates_exhausted(
    payload: dict[str, Any], plan: _ReactionOrcaStagePlan
) -> None:
    """Fail the workflow when every reaction ORCA candidate failed verification.

    The xTB handoff succeeded, so ORCA OptTS+Freq candidate stages were
    materialized, but every one reached a non-verifying terminal state and no
    candidate remains to try. A failed reaction ORCA stage is engine-role
    non-fatal, so nothing else fails the workflow -- without this the reaction TS
    search is reported COMPLETED despite producing no transition state. Mirror the
    scan_ts_search exhaustion guard; the shared ``_all_terminal_none_verified`` also
    excludes an in-progress cancellation (cancelled candidates are not "failed").
    """
    if plan.has_pending_xtb:
        return
    if not _all_terminal_none_verified(plan.existing_stages):
        return
    stage_id = str(plan.existing_stages[0].get("stage_id") or "")
    limit_exhausted = plan.omitted_candidate_count > 0
    stage_count = len(plan.existing_stages)
    rejected_count = sum(
        1
        for stage in plan.existing_stages
        if str(stage.get("status") or "").strip().lower() == "submission_failed"
    )
    if rejected_count == stage_count:
        outcome_clause = (
            f"all {stage_count} candidate submission(s) were rejected before execution "
            "(see each stage's submission_error_detail)"
        )
    elif rejected_count:
        outcome_clause = (
            f"{rejected_count} of {stage_count} candidate submission(s) were rejected before "
            "execution and none of the attempted candidates verified a transition state"
        )
    else:
        outcome_clause = "none of the attempted candidates verified a transition state"
    _record_workflow_error(
        payload,
        scope="reaction_ts_search_orca_candidate_exhausted",
        stage_id=stage_id,
        reason="ts_candidate_limit_exhausted" if limit_exhausted else "ts_candidates_exhausted",
        message=(
            f"The max_orca_stages={plan.max_stage_count} reaction-candidate limit was exhausted; "
            f"{plan.omitted_candidate_count} additional candidate(s) were not submitted and "
            f"{outcome_clause}."
            if limit_exhausted
            else f"All reaction TS candidates reached terminal states; {outcome_clause}."
        ),
    )


def _reaction_orca_stage_plan(
    services: OrchestrationServices,
    payload: dict[str, Any],
    *,
    workspace_dir: Path,
    xtb_config: str | None,
    orca_config: str | None,
) -> _ReactionOrcaStagePlan | None:
    xtb_stages = _completed_or_recoverable_xtb_stages(payload)
    if not xtb_stages:
        return None
    if load_config_root_impl(xtb_config, engine="xtb", services=services) is None:
        return None
    if load_config_root_impl(orca_config, engine="orca", services=services) is None:
        return None
    xtb_runtime_paths = workflow_workspace_internal_engine_paths(workspace_dir, engine="xtb")
    orca_runtime_paths = workflow_workspace_internal_engine_paths(workspace_dir, engine="orca")
    params = workflow_request_parameters(payload)
    payload_metadata_raw = payload.setdefault("metadata", {})
    payload_metadata = payload_metadata_raw if isinstance(payload_metadata_raw, dict) else {}
    candidate_pool, handoff_errors = _collect_reaction_orca_candidates(
        services,
        xtb_stages,
        xtb_allowed_root=xtb_runtime_paths["allowed_root"],
    )
    ordered_candidates = _unique_ordered_candidates(candidate_pool)
    existing = _engine_stages(payload, "orca")
    unattempted_candidates = _remaining_orca_candidates(existing, ordered_candidates)
    max_stage_count = strict_int(
        required_stage_budget(params, "max_orca_stages"),
        field="max_orca_stages",
        minimum=1,
    )
    available_slots = max(0, max_stage_count - len(existing))
    remaining_candidates = unattempted_candidates[:available_slots]
    omitted_candidate_count = len(unattempted_candidates) - len(remaining_candidates)
    return _ReactionOrcaStagePlan(
        payload_metadata=payload_metadata,
        params=params,
        orca_allowed_root=orca_runtime_paths["allowed_root"],
        ordered_candidates=ordered_candidates,
        remaining_candidates=remaining_candidates,
        existing_stages=existing,
        max_stage_count=max_stage_count,
        omitted_candidate_count=omitted_candidate_count,
        has_pending_xtb=_has_pending_xtb_stage(payload),
        handoff_errors=handoff_errors,
    )


def _clear_reaction_orca_handoff_errors(payload_metadata: dict[str, Any]) -> None:
    if not isinstance(payload_metadata.get("workflow_error"), dict):
        return
    _clear_workflow_error_scope(
        payload_metadata,
        {"reaction_ts_search_xtb_handoff", "reaction_ts_search_orca_candidate_exhausted"},
    )


def _build_reaction_orca_stage(
    services: OrchestrationServices,
    payload: dict[str, Any],
    plan: _ReactionOrcaStagePlan,
    *,
    candidate: Any,
    next_index: int,
) -> dict[str, Any]:
    return services.engines.build_materialized_orca_stage(
        workflow_id=str(payload.get("workflow_id", "")),
        template_name="reaction_ts_search",
        stage_id=f"orca_optts_freq_{next_index:02d}",
        stage_key=(
            f"{next_index:02d}_{services.engines.safe_name(candidate.kind, fallback='candidate')}"
        ),
        stage_root_name="",
        workspace_dir=plan.orca_allowed_root,
        input_artifact_kind="xtb_candidate",
        candidate=candidate,
        task_kind="optts_freq",
        route_line=plan.params.get("orca_route_line", "! r2scan-3c OptTS Freq TightSCF"),
        charge=strict_int(plan.params.get("charge", 0), field="charge"),
        multiplicity=strict_int(
            plan.params.get("multiplicity", 1), field="multiplicity", minimum=1
        ),
        max_cores=int(plan.params.get("max_cores", 8) or 8),
        max_memory_gb=int(plan.params.get("max_memory_gb", 32) or 32),
        priority=normalize_queue_priority(plan.params.get("priority")),
        xyz_filename="ts_guess.xyz",
        inp_filename="ts_guess.inp",
        inhess_source_path=str(candidate.metadata.get("hessian_path") or ""),
    ).to_dict()


def _annotate_reaction_orca_stage(
    stage: dict[str, Any],
    plan: _ReactionOrcaStagePlan,
    *,
    next_index: int,
    offset: int,
) -> None:
    stage_metadata = workflow_stage_metadata(stage)
    stage_metadata["reaction_candidate_attempt_index"] = next_index
    stage_metadata["reaction_candidate_pool_size"] = len(plan.ordered_candidates)
    stage_metadata["reaction_candidate_limit"] = plan.max_stage_count
    stage_metadata["reaction_candidates_omitted_by_limit"] = plan.omitted_candidate_count
    stage_metadata["reaction_remaining_candidates_after_this"] = max(
        0,
        len(plan.remaining_candidates) - offset,
    )


def _append_reaction_orca_candidate_stage(
    services: OrchestrationServices,
    payload: dict[str, Any],
    plan: _ReactionOrcaStagePlan,
    *,
    candidate: Any,
    next_index: int,
    offset: int,
) -> None:
    stage = _build_reaction_orca_stage(
        services,
        payload,
        plan,
        candidate=candidate,
        next_index=next_index,
    )
    _annotate_reaction_orca_stage(
        stage,
        plan,
        next_index=next_index,
        offset=offset,
    )
    payload.setdefault("stages", []).append(stage)


def _append_reaction_orca_candidate_stages(
    services: OrchestrationServices,
    payload: dict[str, Any],
    plan: _ReactionOrcaStagePlan,
) -> int:
    created = 0
    starting_index = len(plan.existing_stages)
    for offset, candidate in enumerate(plan.remaining_candidates, start=1):
        _append_reaction_orca_candidate_stage(
            services,
            payload,
            plan,
            candidate=candidate,
            next_index=starting_index + offset,
            offset=offset,
        )
        created += 1
    return created


def append_reaction_orca_stages_impl(
    payload: dict[str, Any],
    *,
    workspace_dir: Path,
    xtb_config: str | None,
    orca_config: str | None,
    services: OrchestrationServices | None = None,
) -> bool:
    resolved = resolve_orchestration_services(services)
    if not phase_finished(payload.get("stages", []), engine="xtb"):
        return False
    plan = _reaction_orca_stage_plan(
        resolved,
        payload,
        workspace_dir=workspace_dir,
        xtb_config=xtb_config,
        orca_config=orca_config,
    )
    if plan is None:
        _maybe_record_reaction_xtb_phase_failure(payload)
        return False

    if not plan.remaining_candidates:
        _record_reaction_handoff_failure(
            plan.payload_metadata,
            existing=plan.existing_stages,
            has_pending_xtb=plan.has_pending_xtb,
            handoff_errors=plan.handoff_errors,
        )
        _maybe_record_reaction_orca_candidates_exhausted(payload, plan)
        return False

    _clear_reaction_orca_handoff_errors(plan.payload_metadata)
    created = _append_reaction_orca_candidate_stages(resolved, payload, plan)
    return created > 0


__all__ = ["append_reaction_orca_stages_impl"]
