from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.queue.generation import new_visible_generation_name
from orca_auto.core.utils import normalize_text, now_utc_iso
from orca_auto.flow.registry import sync_workflow_registry
from orca_auto.flow.templates import DEFAULT_CONFORMER_ORCA_ROUTE_LINE
from orca_auto.flow.xyz_utils import load_xyz_atom_sequence

from .advance import advance_workflow, cancel_materialized_workflow
from .factories import (
    WorkflowFactoryDeps,
)
from .factories import (
    create_conformer_screening_workflow_from_request as _create_conformer_screening_workflow_from_request,
)
from .requests import ConformerScreeningWorkflowRequest


def _workflow_factory_deps() -> WorkflowFactoryDeps:
    # These three read this module's globals at call time so tests can inject
    # deterministic ids, timestamps and registry syncs by patching this module.
    return WorkflowFactoryDeps(
        normalize_text=normalize_text,
        workflow_id_factory=new_visible_generation_name,
        now_utc_iso_fn=now_utc_iso,
        sync_workflow_registry_fn=sync_workflow_registry,
    )


def create_conformer_screening_workflow_from_request(
    request: ConformerScreeningWorkflowRequest,
) -> dict[str, Any]:
    return _create_conformer_screening_workflow_from_request(
        request,
        deps=_workflow_factory_deps(),
    )


def create_conformer_screening_workflow(
    *,
    input_xyz: str,
    workflow_root: str | Path,
    workflow_id: str | None = None,
    scaffold_dir: str | Path | None = None,
    crest_mode: str = "standard",
    priority: int = 10,
    max_cores: int = 8,
    max_memory_gb: int = 32,
    max_orca_stages: int = 20,
    orca_route_line: str = DEFAULT_CONFORMER_ORCA_ROUTE_LINE,
    charge: int = 0,
    multiplicity: int = 1,
    boltzmann_temperature_k: float | None = None,
    crest_job_manifest: dict[str, Any] | None = None,
    interaction_energy: dict[str, Any] | None = None,
    rmsd_dedup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return create_conformer_screening_workflow_from_request(
        ConformerScreeningWorkflowRequest(
            input_xyz=input_xyz,
            workflow_root=workflow_root,
            workflow_id=workflow_id,
            scaffold_dir=scaffold_dir,
            crest_mode=crest_mode,
            priority=priority,
            max_cores=max_cores,
            max_memory_gb=max_memory_gb,
            max_orca_stages=max_orca_stages,
            orca_route_line=orca_route_line,
            charge=charge,
            multiplicity=multiplicity,
            boltzmann_temperature_k=boltzmann_temperature_k,
            crest_job_manifest=crest_job_manifest,
            interaction_energy=interaction_energy,
            rmsd_dedup=rmsd_dedup,
        )
    )


__all__ = [
    "ConformerScreeningWorkflowRequest",
    "advance_workflow",
    "cancel_materialized_workflow",
    "create_conformer_screening_workflow",
    "create_conformer_screening_workflow_from_request",
    "load_xyz_atom_sequence",
]
