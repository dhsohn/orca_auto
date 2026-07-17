from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.flow.manifest import require_crest_candidate_count
from orca_auto.flow.orchestration.charge_spin import manifest_with_charge_spin, strict_int
from orca_auto.flow.orchestration.dep_types import OrchestrationDeps
from orca_auto.flow.orchestration.deps import (
    orchestration_context as _orchestration_context,
)
from orca_auto.flow.orchestration.reaction_orca_materialization import (
    append_reaction_orca_stages_impl as append_reaction_orca_stages_impl,
)
from orca_auto.flow.orchestration.stage_views import (
    WorkflowStageView,
    _clear_workflow_error_scope,
    _engine_stages,
    _request_params,
)


@dataclass(frozen=True)
class _ReactionXtbStagePlan:
    params: dict[str, Any]
    endpoint_pairs: tuple[Any, ...]
    pairing_enabled: bool


def _xtb_manifest_with_charge_spin(o: Any, params: dict[str, Any]) -> dict[str, Any] | None:
    return manifest_with_charge_spin(
        charge=params.get("charge"),
        multiplicity=params.get("multiplicity"),
        manifest_overrides=o.stages.support._coerce_mapping(params.get("xtb_job_manifest")),
    )


def _record_endpoint_pairing_summary(
    o: Any,
    payload: dict[str, Any],
    pairing_policy: Any,
    *,
    candidate_pair_count: int,
    selected_pair_count: int,
) -> None:
    if not pairing_policy.enabled:
        return
    payload_metadata = payload.setdefault("metadata", {})
    if not isinstance(payload_metadata, dict):
        return
    payload_metadata["endpoint_pairing"] = {
        **pairing_policy.to_summary(),
        "candidate_pair_count": candidate_pair_count,
        "selected_pair_count": selected_pair_count,
    }
    if selected_pair_count:
        _clear_workflow_error_scope(o, payload_metadata, {"reaction_ts_search_endpoint_pairing"})


def _record_endpoint_pairing_failure(payload: dict[str, Any]) -> None:
    payload_metadata = payload.setdefault("metadata", {})
    if isinstance(payload_metadata, dict) and not isinstance(
        payload_metadata.get("workflow_error"), dict
    ):
        payload_metadata["workflow_error"] = {
            "status": "failed",
            "scope": "reaction_ts_search_endpoint_pairing",
            "reason": "no_endpoint_pairs",
            "message": "No CREST reactant/product conformer pair passed endpoint pairing filters.",
        }


def _record_crest_handoff_failure(
    payload: dict[str, Any],
    *,
    missing_roles: tuple[str, ...],
) -> None:
    payload_metadata = payload.setdefault("metadata", {})
    if isinstance(payload_metadata, dict) and not isinstance(
        payload_metadata.get("workflow_error"), dict
    ):
        roles = ", ".join(missing_roles)
        payload_metadata["workflow_error"] = {
            "status": "failed",
            "scope": "reaction_ts_search_crest_handoff",
            "reason": "crest_no_usable_conformers",
            "message": f"Completed CREST stage(s) have no usable retained geometry: {roles}.",
        }


def _completed_reaction_crest_contracts(
    o: Any,
    payload: dict[str, Any],
    *,
    crest_config: str | None,
) -> tuple[Any, Any] | None:
    roles = o.stages.runtime._completed_crest_roles(payload)
    if set(roles.keys()) != {"reactant", "product"}:
        return None
    reactant_contract = o.stages.runtime._completed_crest_stage(
        roles["reactant"], crest_config=crest_config
    )
    product_contract = o.stages.runtime._completed_crest_stage(
        roles["product"], crest_config=crest_config
    )
    missing_roles = tuple(
        role
        for role, contract in (
            ("reactant", reactant_contract),
            ("product", product_contract),
        )
        if contract is None
    )
    if missing_roles:
        _record_crest_handoff_failure(payload, missing_roles=missing_roles)
        return None
    return reactant_contract, product_contract


def _reaction_xtb_stage_plan(
    o: Any,
    payload: dict[str, Any],
    *,
    crest_config: str | None,
) -> _ReactionXtbStagePlan | None:
    contracts = _completed_reaction_crest_contracts(
        o,
        payload,
        crest_config=crest_config,
    )
    if contracts is None:
        return None
    reactant_contract, product_contract = contracts
    params = _request_params(o, payload)
    max_crest_candidates = require_crest_candidate_count(
        params.get("max_crest_candidates", 3),
    )
    reactant_inputs = o.engines.select_crest_downstream_inputs(
        reactant_contract,
        policy=o.contracts.CrestDownstreamPolicy.build(max_candidates=max_crest_candidates),
    )
    product_inputs = o.engines.select_crest_downstream_inputs(
        product_contract,
        policy=o.contracts.CrestDownstreamPolicy.build(max_candidates=max_crest_candidates),
    )
    missing_roles = tuple(
        role
        for role, inputs in (("reactant", reactant_inputs), ("product", product_inputs))
        if not inputs
    )
    if missing_roles:
        _record_crest_handoff_failure(payload, missing_roles=missing_roles)
        return None
    # Creation always persists the explicit value; this fallback fires only
    # for legacy/hand-edited payloads missing the key and stays at the old
    # default so an upgrade never retroactively grows a stored workflow.
    max_xtb_stages = strict_int(
        params.get("max_xtb_stages", 3),
        field="max_xtb_stages",
        minimum=1,
    )
    pairing_policy = o.contracts.EndpointPairingPolicy.from_raw(
        params.get("endpoint_pairing"),
        default_max_pairs=max_xtb_stages,
    )
    pairing_policy = replace(
        pairing_policy,
        max_pairs=(
            min(pairing_policy.max_pairs, max_xtb_stages)
            if pairing_policy.max_pairs
            else max_xtb_stages
        ),
    )
    endpoint_pairs = tuple(
        o.engines.select_endpoint_pairs(
            reactant_inputs,
            product_inputs,
            policy=pairing_policy,
        )[:max_xtb_stages]
    )
    candidate_pair_count = len(reactant_inputs) * len(product_inputs)
    _record_endpoint_pairing_summary(
        o,
        payload,
        pairing_policy,
        candidate_pair_count=candidate_pair_count,
        selected_pair_count=len(endpoint_pairs),
    )
    if pairing_policy.enabled and not endpoint_pairs:
        _record_endpoint_pairing_failure(payload)
        return None
    return _ReactionXtbStagePlan(
        params=params,
        endpoint_pairs=endpoint_pairs,
        pairing_enabled=pairing_policy.enabled,
    )


def append_reaction_xtb_stages_impl(
    payload: dict[str, Any],
    *,
    workspace_dir: Path,
    crest_config: str | None,
    deps: OrchestrationDeps | None = None,
) -> bool:
    o = _orchestration_context(deps)
    if _engine_stages(o, payload, "xtb"):
        return False
    del workspace_dir
    plan = _reaction_xtb_stage_plan(o, payload, crest_config=crest_config)
    if plan is None:
        return False

    created = 0
    for endpoint_pair in plan.endpoint_pairs:
        created += 1
        stage = o.stages.builders._new_xtb_stage(
            workflow_id=str(payload.get("workflow_id", "")),
            stage_id=f"xtb_path_search_{created:02d}",
            reaction_key=f"{payload.get('reaction_key', 'reaction')}_{created:02d}",
            reactant_input=endpoint_pair.reactant.to_dict(),
            product_input=endpoint_pair.product.to_dict(),
            priority=normalize_queue_priority(plan.params.get("priority")),
            max_cores=int(plan.params.get("max_cores", 8) or 8),
            max_memory_gb=int(plan.params.get("max_memory_gb", 32) or 32),
            max_handoff_retries=strict_int(
                plan.params.get("max_xtb_handoff_retries", 2),
                field="max_xtb_handoff_retries",
                minimum=0,
            ),
            manifest_overrides=_xtb_manifest_with_charge_spin(o, plan.params),
        )
        if plan.pairing_enabled:
            pairing_metadata = dict(endpoint_pair.metadata)
            stage_view = WorkflowStageView(stage)
            stage_metadata = stage_view.metadata(o)
            stage_metadata["endpoint_pairing"] = pairing_metadata
            if stage_view.has_task:
                stage_view.task.metadata(o)["endpoint_pairing"] = pairing_metadata
        payload.setdefault("stages", []).append(stage)
    return created > 0
