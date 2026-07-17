from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from orca_auto.core.queue.generation import new_visible_generation_name
from orca_auto.core.utils import now_utc_iso
from orca_auto.flow.endpoint_pairing import (
    EndpointPairingPolicy,
    validate_endpoint_pairing_atom_budget,
)
from orca_auto.flow.manifest import (
    normalize_interaction_energy_block,
    normalize_rmsd_dedup_block,
    optional_positive_float,
    require_crest_candidate_count,
    validate_interaction_energy_state_balance,
)
from orca_auto.flow.orchestration.builders import (
    create_conformer_screening_workflow_impl,
    create_reaction_ts_search_workflow_impl,
    create_scan_ts_search_workflow_impl,
)
from orca_auto.flow.orchestration.charge_spin import strict_int
from orca_auto.flow.orchestration.requests import (
    ConformerScreeningWorkflowRequest,
    NewCrestStageFactory,
    ReactionTsSearchWorkflowCreationContext,
    ReactionTsSearchWorkflowRequest,
    ScanTsSearchWorkflowRequest,
    WorkflowCreationContext,
)
from orca_auto.flow.orchestration.stage_builders import new_crest_stage_impl
from orca_auto.flow.orchestration.workflow_builders import _copy_input_impl
from orca_auto.flow.registry import sync_workflow_registry
from orca_auto.flow.state import write_workflow_payload
from orca_auto.flow.xyz_utils import load_xyz_atom_sequence
from orca_auto.orca.report.interaction_energy import (
    validate_fragment_electronic_states,
    validate_fragment_partition,
)


@dataclass(frozen=True)
class WorkflowFactoryDeps:
    normalize_text: Callable[[Any], str]
    workflow_id_factory: Callable[[], str] = new_visible_generation_name
    copy_input_fn: Callable[[str, Path], str] = _copy_input_impl
    now_utc_iso_fn: Callable[[], str] = now_utc_iso
    new_crest_stage_fn: NewCrestStageFactory = new_crest_stage_impl
    write_workflow_payload_fn: Callable[[Path, dict[str, Any]], Any] = write_workflow_payload
    sync_workflow_registry_fn: Callable[[Path, Path, dict[str, Any]], Any] = sync_workflow_registry
    load_xyz_atom_sequence_fn: Callable[[str], tuple[str, ...]] = load_xyz_atom_sequence

    def workflow_context(self) -> WorkflowCreationContext:
        return WorkflowCreationContext(
            workflow_id_factory=self.workflow_id_factory,
            copy_input_fn=self.copy_input_fn,
            now_utc_iso_fn=self.now_utc_iso_fn,
            new_crest_stage_fn=self.new_crest_stage_fn,
            write_workflow_payload_fn=self.write_workflow_payload_fn,
            sync_workflow_registry_fn=self.sync_workflow_registry_fn,
        )

    def reaction_ts_context(self) -> ReactionTsSearchWorkflowCreationContext:
        return ReactionTsSearchWorkflowCreationContext(
            workflow_id_factory=self.workflow_id_factory,
            copy_input_fn=self.copy_input_fn,
            now_utc_iso_fn=self.now_utc_iso_fn,
            new_crest_stage_fn=self.new_crest_stage_fn,
            write_workflow_payload_fn=self.write_workflow_payload_fn,
            sync_workflow_registry_fn=self.sync_workflow_registry_fn,
            load_xyz_atom_sequence_fn=self.load_xyz_atom_sequence_fn,
        )


def _positive_int_field(value: Any, *, field_name: str) -> int:
    return strict_int(value, field=field_name, minimum=1)


def _positive_float_field(value: Any, *, field_name: str) -> float:
    parsed = optional_positive_float({field_name: value}, field_name)
    if parsed is None:
        raise ValueError(f"{field_name} must be a positive finite number. got={value!r}")
    return parsed


def _normalized_reaction_ts_request(
    request: ReactionTsSearchWorkflowRequest,
    *,
    deps: WorkflowFactoryDeps,
) -> ReactionTsSearchWorkflowRequest:
    normalized_crest_mode = deps.normalize_text(request.crest_mode).lower()
    if normalized_crest_mode not in {"standard", "nci"}:
        raise ValueError("reaction_ts_search only supports crest_mode 'standard' or 'nci'")
    max_crest_candidates = require_crest_candidate_count(
        request.max_crest_candidates,
    )
    max_xtb_stages = _positive_int_field(
        request.max_xtb_stages,
        field_name="max_xtb_stages",
    )
    pairing_policy = EndpointPairingPolicy.from_raw(
        request.endpoint_pairing,
        default_max_pairs=max_xtb_stages,
    )
    if pairing_policy.enabled and (
        pairing_policy.comparison_atoms
        or pairing_policy.excluded_atoms
        or pairing_policy.max_distance_rmsd is not None
    ):
        validate_endpoint_pairing_atom_budget(
            pairing_policy,
            len(deps.load_xyz_atom_sequence_fn(request.reactant_xyz)),
            len(deps.load_xyz_atom_sequence_fn(request.product_xyz)),
        )
    return replace(
        request,
        crest_mode=normalized_crest_mode,
        charge=strict_int(request.charge, field="charge"),
        max_cores=_positive_int_field(request.max_cores, field_name="max_cores"),
        max_memory_gb=_positive_int_field(
            request.max_memory_gb,
            field_name="max_memory_gb",
        ),
        max_crest_candidates=max_crest_candidates,
        max_xtb_stages=max_xtb_stages,
        max_xtb_handoff_retries=strict_int(
            request.max_xtb_handoff_retries,
            field="max_xtb_handoff_retries",
            minimum=0,
        ),
        max_orca_stages=_positive_int_field(
            request.max_orca_stages,
            field_name="max_orca_stages",
        ),
        multiplicity=_positive_int_field(request.multiplicity, field_name="multiplicity"),
    )


def _normalized_conformer_screening_request(
    request: ConformerScreeningWorkflowRequest,
    *,
    deps: WorkflowFactoryDeps,
) -> ConformerScreeningWorkflowRequest:
    normalized_crest_mode = deps.normalize_text(request.crest_mode).lower()
    if normalized_crest_mode not in {"standard", "nci"}:
        raise ValueError("conformer_screening only supports crest_mode 'standard' or 'nci'")
    charge = strict_int(request.charge, field="charge")
    multiplicity = strict_int(request.multiplicity, field="multiplicity", minimum=1)
    interaction_energy = normalize_interaction_energy_block(request.interaction_energy)
    validate_interaction_energy_state_balance(
        interaction_energy,
        complex_charge=charge,
        complex_multiplicity=multiplicity,
    )
    if interaction_energy is not None:
        atom_symbols = deps.load_xyz_atom_sequence_fn(request.input_xyz)
        atom_count = len(atom_symbols)
        partition_reason = validate_fragment_partition(
            [fragment["atom_indices"] for fragment in interaction_energy["fragments"]],
            atom_count,
        )
        if partition_reason:
            raise ValueError(
                f"interaction_energy fragments do not partition input.xyz: {partition_reason}"
            )
        state_reason = validate_fragment_electronic_states(
            atom_symbols, interaction_energy["fragments"]
        )
        if state_reason:
            raise ValueError(f"interaction_energy fragment state is impossible: {state_reason}")
    return replace(
        request,
        crest_mode=normalized_crest_mode,
        charge=charge,
        max_cores=_positive_int_field(request.max_cores, field_name="max_cores"),
        max_memory_gb=_positive_int_field(
            request.max_memory_gb,
            field_name="max_memory_gb",
        ),
        max_orca_stages=_positive_int_field(
            request.max_orca_stages,
            field_name="max_orca_stages",
        ),
        multiplicity=multiplicity,
        boltzmann_temperature_k=optional_positive_float(
            {"boltzmann_temperature_k": request.boltzmann_temperature_k},
            "boltzmann_temperature_k",
        ),
        interaction_energy=interaction_energy,
        rmsd_dedup=normalize_rmsd_dedup_block(request.rmsd_dedup),
    )


def create_reaction_ts_search_workflow_from_request(
    request: ReactionTsSearchWorkflowRequest,
    *,
    deps: WorkflowFactoryDeps,
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        create_reaction_ts_search_workflow_impl(
            request=_normalized_reaction_ts_request(request, deps=deps),
            context=deps.reaction_ts_context(),
        ),
    )


def create_conformer_screening_workflow_from_request(
    request: ConformerScreeningWorkflowRequest,
    *,
    deps: WorkflowFactoryDeps,
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        create_conformer_screening_workflow_impl(
            request=_normalized_conformer_screening_request(request, deps=deps),
            context=deps.workflow_context(),
        ),
    )


def _normalized_scan_ts_request(
    request: ScanTsSearchWorkflowRequest,
    *,
    deps: WorkflowFactoryDeps,
) -> ScanTsSearchWorkflowRequest:
    scan_coordinate = deps.normalize_text(request.scan_coordinate)
    if "=" not in scan_coordinate or "," not in scan_coordinate:
        raise ValueError(
            "scan_ts_search requires scan_coordinate like 'B 20 61 = 1.80, 5.00, 32'. "
            f"got={request.scan_coordinate!r}"
        )
    threshold = _positive_float_field(
        request.barrier_threshold_kcal,
        field_name="barrier_threshold_kcal",
    )
    max_scan_extensions = strict_int(
        request.max_scan_extensions,
        field="max_scan_extensions",
        minimum=0,
    )
    return replace(
        request,
        scan_coordinate=scan_coordinate,
        charge=strict_int(request.charge, field="charge"),
        barrier_threshold_kcal=threshold,
        max_scan_extensions=max_scan_extensions,
        max_cores=_positive_int_field(request.max_cores, field_name="max_cores"),
        max_memory_gb=_positive_int_field(
            request.max_memory_gb,
            field_name="max_memory_gb",
        ),
        max_orca_stages=_positive_int_field(
            request.max_orca_stages,
            field_name="max_orca_stages",
        ),
        multiplicity=_positive_int_field(request.multiplicity, field_name="multiplicity"),
    )


def create_scan_ts_search_workflow_from_request(
    request: ScanTsSearchWorkflowRequest,
    *,
    deps: WorkflowFactoryDeps,
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        create_scan_ts_search_workflow_impl(
            request=_normalized_scan_ts_request(request, deps=deps),
            context=deps.workflow_context(),
        ),
    )


__all__ = [
    "ConformerScreeningWorkflowRequest",
    "ReactionTsSearchWorkflowRequest",
    "ScanTsSearchWorkflowRequest",
    "WorkflowFactoryDeps",
    "create_conformer_screening_workflow_from_request",
    "create_reaction_ts_search_workflow_from_request",
    "create_scan_ts_search_workflow_from_request",
]
