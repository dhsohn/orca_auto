from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.statuses import is_queue_active_status
from orca_auto.flow._workflow_phases import phase_finished
from orca_auto.flow.orchestration.dep_types import OrchestrationDeps
from orca_auto.flow.orchestration.deps import (
    orchestration_context as _orchestration_context,
)
from orca_auto.flow.orchestration.stage_views import (
    WorkflowStageView,
    _clear_workflow_error_scope,
    _engine_stage_views,
    _engine_stages,
    _request_params,
    _stage_views,
)
from orca_auto.flow.state import workflow_workspace_internal_engine_paths


@dataclass(frozen=True)
class _ReactionOrcaStagePlan:
    payload_metadata: dict[str, Any]
    params: dict[str, Any]
    orca_allowed_root: Path
    ordered_candidates: list[Any]
    remaining_candidates: list[Any]
    existing_stages: list[dict[str, Any]]
    has_pending_xtb: bool
    handoff_errors: list[dict[str, str]]


def _completed_or_recoverable_xtb_stages(o: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        view.raw
        for view in _engine_stage_views(o, payload, "xtb")
        if view.status(o) == "completed"
        or o.stages.workflow._stage_failure_is_recoverable(view.raw)
    ]


def _load_xtb_contract_for_stage(
    o: Any, xtb_stage: dict[str, Any], *, xtb_allowed_root: Path
) -> Any | None:
    view = WorkflowStageView.from_raw(xtb_stage)
    if view is None or not view.task.raw:
        return None
    payload_dict = o.stages.support._task_payload_dict(view.task.raw)
    target = o.stages.support._normalize_text(
        payload_dict.get("job_dir")
    ) or o.stages.support._submission_target(xtb_stage)
    if not target:
        return None
    try:
        return o.engines.load_xtb_artifact_contract(xtb_index_root=xtb_allowed_root, target=target)
    except FileNotFoundError:
        return None


def _xtb_ts_guess_inputs(o: Any, contract: Any) -> list[Any]:
    max_candidate_count = max(
        1,
        len(getattr(contract, "candidate_details", ()) or ()),
        len(getattr(contract, "selected_candidate_paths", ()) or ()),
    )
    return o.engines.select_xtb_downstream_inputs(
        contract,
        policy=o.contracts.XtbDownstreamPolicy.build(
            preferred_kinds=("ts_guess",),
            allowed_kinds=("ts_guess",),
            max_candidates=max_candidate_count,
            selected_only=False,
        ),
        require_geometry=True,
    )


def _record_xtb_handoff_error(o: Any, xtb_stage: dict[str, Any], contract: Any) -> dict[str, str]:
    stage_metadata = o.stages.support._stage_metadata(xtb_stage)
    error = o.stages.support._reaction_ts_guess_error(contract)
    stage_metadata["reaction_handoff_status"] = "failed"
    stage_metadata["reaction_handoff_reason"] = error["reason"]
    stage_metadata["reaction_handoff_message"] = error["message"]
    return {
        "stage_id": WorkflowStageView(xtb_stage).stage_id(o),
        "job_id": o.stages.support._normalize_text(getattr(contract, "job_id", "")),
        "reason": error["reason"],
        "message": error["message"],
    }


def _mark_xtb_handoff_ready(o: Any, xtb_stage: dict[str, Any]) -> None:
    stage_metadata = o.stages.support._stage_metadata(xtb_stage)
    stage_metadata.pop("reaction_handoff_reason", None)
    stage_metadata.pop("reaction_handoff_message", None)
    stage_metadata["reaction_handoff_status"] = "ready"


def _reaction_orca_candidate_pool_rows(
    o: Any, xtb_stage: dict[str, Any], contract: Any, inputs: list[Any], stage_order: int
) -> list[tuple[int, int, str, Any]]:
    rows: list[tuple[int, int, str, Any]] = []
    for candidate in inputs:
        candidate_path = o.stages.support._normalize_text(candidate.artifact_path)
        if not candidate_path:
            continue
        candidate_metadata = {
            **dict(candidate.metadata),
            "xtb_stage_id": WorkflowStageView(xtb_stage).stage_id(o),
            "xtb_stage_order": int(stage_order),
            "xtb_source_job_id": o.stages.support._normalize_text(getattr(contract, "job_id", "")),
            "xtb_source_job_type": o.stages.support._normalize_text(
                getattr(contract, "job_type", "")
            ),
        }
        rows.append(
            (
                int(stage_order),
                candidate.rank if candidate.rank > 0 else 10_000,
                candidate_path,
                o.contracts.WorkflowStageInput(
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
    o: Any,
    xtb_stages: list[dict[str, Any]],
    *,
    xtb_allowed_root: Path,
) -> tuple[list[tuple[int, int, str, Any]], list[dict[str, str]]]:
    candidate_pool: list[tuple[int, int, str, Any]] = []
    handoff_errors: list[dict[str, str]] = []
    for xtb_stage_index, xtb_stage in enumerate(xtb_stages, start=1):
        contract = _load_xtb_contract_for_stage(o, xtb_stage, xtb_allowed_root=xtb_allowed_root)
        if contract is None:
            continue
        inputs = _xtb_ts_guess_inputs(o, contract)
        if not inputs:
            handoff_errors.append(_record_xtb_handoff_error(o, xtb_stage, contract))
            continue
        _mark_xtb_handoff_ready(o, xtb_stage)
        candidate_pool.extend(
            _reaction_orca_candidate_pool_rows(o, xtb_stage, contract, inputs, xtb_stage_index)
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


def _has_pending_xtb_stage(o: Any, payload: dict[str, Any]) -> bool:
    return any(
        view.task_engine(o) == "xtb"
        and (is_queue_active_status(view.status(o)) or is_queue_active_status(view.task_status(o)))
        for view in _stage_views(payload)
    )


def _remaining_orca_candidates(
    o: Any, existing: list[dict[str, Any]], ordered_candidates: list[Any]
) -> list[Any]:
    attempted_paths = {
        o.stages.support._reaction_orca_source_candidate_path(stage)
        for stage in existing
        if o.stages.support._reaction_orca_source_candidate_path(stage)
    }
    return [
        candidate
        for candidate in ordered_candidates
        if o.stages.support._normalize_text(candidate.artifact_path) not in attempted_paths
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


def _reaction_orca_stage_plan(
    o: Any,
    payload: dict[str, Any],
    *,
    workspace_dir: Path,
    xtb_config: str | None,
    orca_config: str | None,
) -> _ReactionOrcaStagePlan | None:
    xtb_stages = _completed_or_recoverable_xtb_stages(o, payload)
    if not xtb_stages:
        return None
    xtb_allowed_root = o.stages.support._load_config_root(xtb_config, engine="xtb")
    if xtb_allowed_root is None:
        return None
    if o.stages.support._load_config_root(orca_config, engine="orca") is None:
        return None
    orca_runtime_paths = workflow_workspace_internal_engine_paths(workspace_dir, engine="orca")
    params = _request_params(o, payload)
    payload_metadata_raw = payload.setdefault("metadata", {})
    payload_metadata = payload_metadata_raw if isinstance(payload_metadata_raw, dict) else {}
    candidate_pool, handoff_errors = _collect_reaction_orca_candidates(
        o,
        xtb_stages,
        xtb_allowed_root=xtb_allowed_root,
    )
    ordered_candidates = _unique_ordered_candidates(candidate_pool)
    existing = _engine_stages(o, payload, "orca")
    remaining_candidates = _remaining_orca_candidates(o, existing, ordered_candidates)
    return _ReactionOrcaStagePlan(
        payload_metadata=payload_metadata,
        params=params,
        orca_allowed_root=orca_runtime_paths["allowed_root"],
        ordered_candidates=ordered_candidates,
        remaining_candidates=remaining_candidates,
        existing_stages=existing,
        has_pending_xtb=_has_pending_xtb_stage(o, payload),
        handoff_errors=handoff_errors,
    )


def _clear_reaction_orca_handoff_errors(o: Any, payload_metadata: dict[str, Any]) -> None:
    if not isinstance(payload_metadata.get("workflow_error"), dict):
        return
    _clear_workflow_error_scope(
        o,
        payload_metadata,
        {"reaction_ts_search_xtb_handoff", "reaction_ts_search_orca_candidate_exhausted"},
    )


def _build_reaction_orca_stage(
    o: Any,
    payload: dict[str, Any],
    plan: _ReactionOrcaStagePlan,
    *,
    candidate: Any,
    next_index: int,
) -> dict[str, Any]:
    return o.engines.build_materialized_orca_stage(
        workflow_id=str(payload.get("workflow_id", "")),
        template_name="reaction_ts_search",
        stage_id=f"orca_optts_freq_{next_index:02d}",
        stage_key=f"{next_index:02d}_{o.engines.safe_name(candidate.kind, fallback='candidate')}",
        stage_root_name="",
        workspace_dir=plan.orca_allowed_root,
        input_artifact_kind="xtb_candidate",
        candidate=candidate,
        task_kind="optts_freq",
        route_line=str(plan.params.get("orca_route_line", "! r2scan-3c OptTS Freq TightSCF")),
        charge=int(plan.params.get("charge", 0) or 0),
        multiplicity=int(plan.params.get("multiplicity", 1) or 1),
        max_cores=int(plan.params.get("max_cores", 8) or 8),
        max_memory_gb=int(plan.params.get("max_memory_gb", 32) or 32),
        priority=int(plan.params.get("priority", 10) or 10),
        xyz_filename="ts_guess.xyz",
        inp_filename="ts_guess.inp",
    ).to_dict()


def _annotate_reaction_orca_stage(
    o: Any,
    stage: dict[str, Any],
    plan: _ReactionOrcaStagePlan,
    *,
    next_index: int,
    offset: int,
) -> None:
    stage_metadata = o.stages.support._stage_metadata(stage)
    stage_metadata["reaction_candidate_attempt_index"] = next_index
    stage_metadata["reaction_candidate_pool_size"] = len(plan.ordered_candidates)
    stage_metadata["reaction_remaining_candidates_after_this"] = max(
        0,
        len(plan.remaining_candidates) - offset,
    )


def _append_reaction_orca_candidate_stage(
    o: Any,
    payload: dict[str, Any],
    plan: _ReactionOrcaStagePlan,
    *,
    candidate: Any,
    next_index: int,
    offset: int,
) -> None:
    stage = _build_reaction_orca_stage(
        o,
        payload,
        plan,
        candidate=candidate,
        next_index=next_index,
    )
    _annotate_reaction_orca_stage(
        o,
        stage,
        plan,
        next_index=next_index,
        offset=offset,
    )
    payload.setdefault("stages", []).append(stage)


def _append_reaction_orca_candidate_stages(
    o: Any,
    payload: dict[str, Any],
    plan: _ReactionOrcaStagePlan,
) -> int:
    created = 0
    starting_index = len(plan.existing_stages)
    for offset, candidate in enumerate(plan.remaining_candidates, start=1):
        _append_reaction_orca_candidate_stage(
            o,
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
    deps: OrchestrationDeps | None = None,
) -> bool:
    o = _orchestration_context(deps)
    if not phase_finished(payload.get("stages", []), engine="xtb"):
        return False
    plan = _reaction_orca_stage_plan(
        o,
        payload,
        workspace_dir=workspace_dir,
        xtb_config=xtb_config,
        orca_config=orca_config,
    )
    if plan is None:
        return False

    if not plan.remaining_candidates:
        _record_reaction_handoff_failure(
            plan.payload_metadata,
            existing=plan.existing_stages,
            has_pending_xtb=plan.has_pending_xtb,
            handoff_errors=plan.handoff_errors,
        )
        return False

    _clear_reaction_orca_handoff_errors(o, plan.payload_metadata)
    created = _append_reaction_orca_candidate_stages(o, payload, plan)
    return created > 0


__all__ = ["append_reaction_orca_stages_impl"]
