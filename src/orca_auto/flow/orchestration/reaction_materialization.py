from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.flow.orchestration.charge_spin import manifest_with_charge_spin
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
    if isinstance(payload_metadata, dict):
        payload_metadata["workflow_error"] = {
            "status": "failed",
            "scope": "reaction_ts_search_endpoint_pairing",
            "reason": "no_endpoint_pairs",
            "message": "No CREST reactant/product conformer pair passed endpoint pairing filters.",
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
    if reactant_contract is None or product_contract is None:
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
    reactant_inputs = o.engines.select_crest_downstream_inputs(
        reactant_contract,
        policy=o.contracts.CrestDownstreamPolicy.build(
            max_candidates=int(params.get("max_crest_candidates", 3) or 3)
        ),
    )
    product_inputs = o.engines.select_crest_downstream_inputs(
        product_contract,
        policy=o.contracts.CrestDownstreamPolicy.build(
            max_candidates=int(params.get("max_crest_candidates", 3) or 3)
        ),
    )
    pairing_policy = o.contracts.EndpointPairingPolicy.from_raw(
        params.get("endpoint_pairing"),
        default_max_pairs=int(params.get("max_xtb_stages", 0) or 0),
    )
    endpoint_pairs = o.engines.select_endpoint_pairs(
        reactant_inputs,
        product_inputs,
        policy=pairing_policy,
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
            priority=int(plan.params.get("priority", 10) or 10),
            max_cores=int(plan.params.get("max_cores", 8) or 8),
            max_memory_gb=int(plan.params.get("max_memory_gb", 32) or 32),
            max_handoff_retries=int(plan.params.get("max_xtb_handoff_retries", 2) or 2),
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
