from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from orca_auto.core.utils import now_utc_iso, timestamped_token
from orca_auto.flow.manifest import optional_positive_float
from orca_auto.flow.orchestration.builders import (
    create_conformer_screening_workflow_impl,
    create_reaction_ts_search_workflow_impl,
    create_scan_ts_search_workflow_impl,
)
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


@dataclass(frozen=True)
class WorkflowFactoryDeps:
    normalize_text: Callable[[Any], str]
    workflow_id_factory: Callable[[str], str] = timestamped_token
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
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer >= 1. got={value!r}") from exc
    if parsed < 1:
        raise ValueError(f"{field_name} must be >= 1. got={parsed}")
    return parsed


def _normalized_reaction_ts_request(
    request: ReactionTsSearchWorkflowRequest,
    *,
    deps: WorkflowFactoryDeps,
) -> ReactionTsSearchWorkflowRequest:
    normalized_crest_mode = deps.normalize_text(request.crest_mode).lower()
    if normalized_crest_mode not in {"standard", "nci"}:
        raise ValueError("reaction_ts_search only supports crest_mode 'standard' or 'nci'")
    return replace(
        request,
        crest_mode=normalized_crest_mode,
        max_cores=_positive_int_field(request.max_cores, field_name="max_cores"),
        max_memory_gb=_positive_int_field(
            request.max_memory_gb,
            field_name="max_memory_gb",
        ),
        max_crest_candidates=_positive_int_field(
            request.max_crest_candidates,
            field_name="max_crest_candidates",
        ),
        max_xtb_stages=_positive_int_field(
            request.max_xtb_stages,
            field_name="max_xtb_stages",
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
    return replace(
        request,
        crest_mode=normalized_crest_mode,
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
        boltzmann_temperature_k=optional_positive_float(
            {"boltzmann_temperature_k": request.boltzmann_temperature_k},
            "boltzmann_temperature_k",
        ),
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
    threshold = float(request.barrier_threshold_kcal)
    if threshold <= 0:
        raise ValueError(f"barrier_threshold_kcal must be > 0. got={threshold}")
    max_scan_extensions = int(request.max_scan_extensions)
    if max_scan_extensions < 0:
        raise ValueError(f"max_scan_extensions must be >= 0. got={max_scan_extensions}")
    return replace(
        request,
        scan_coordinate=scan_coordinate,
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
