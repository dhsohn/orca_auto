from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from orca_auto.flow.contracts.workflow import WorkflowStageWithTaskPayload
from orca_auto.flow.templates import (
    DEFAULT_CONFORMER_ORCA_ROUTE_LINE,
    DEFAULT_REACTION_TS_ORCA_ROUTE_LINE,
    DEFAULT_SCAN_OPTTS_ORCA_ROUTE_LINE,
    DEFAULT_SCAN_ORCA_ROUTE_LINE,
)


@dataclass(frozen=True)
class ReactionTsSearchWorkflowRequest:
    reactant_xyz: str
    product_xyz: str
    workflow_root: str | Path
    workflow_id: str | None = None
    crest_mode: str = "standard"
    priority: int = 10
    max_cores: int = 8
    max_memory_gb: int = 32
    max_crest_candidates: int = 3
    max_xtb_stages: int = 9
    max_xtb_handoff_retries: int = 2
    max_orca_stages: int = 3
    orca_route_line: str = DEFAULT_REACTION_TS_ORCA_ROUTE_LINE
    charge: int = 0
    multiplicity: int = 1
    crest_job_manifest: dict[str, Any] | None = None
    xtb_job_manifest: dict[str, Any] | None = None
    endpoint_pairing: dict[str, Any] | None = None
    source_job_id: str = ""
    source_job_type: str = ""
    scaffold_dir: str | Path | None = None


@dataclass(frozen=True)
class ConformerScreeningWorkflowRequest:
    input_xyz: str
    workflow_root: str | Path
    workflow_id: str | None = None
    crest_mode: str = "standard"
    priority: int = 10
    max_cores: int = 8
    max_memory_gb: int = 32
    max_orca_stages: int = 20
    orca_route_line: str = DEFAULT_CONFORMER_ORCA_ROUTE_LINE
    charge: int = 0
    multiplicity: int = 1
    crest_job_manifest: dict[str, Any] | None = None
    boltzmann_temperature_k: float | None = None
    interaction_energy: dict[str, Any] | None = None
    rmsd_dedup: dict[str, Any] | None = None
    scaffold_dir: str | Path | None = None


@dataclass(frozen=True)
class ScanTsSearchWorkflowRequest:
    input_xyz: str
    scan_coordinate: str
    workflow_root: str | Path
    workflow_id: str | None = None
    priority: int = 10
    max_cores: int = 8
    max_memory_gb: int = 32
    max_orca_stages: int = 5
    max_scan_extensions: int = 1
    orca_route_line: str = DEFAULT_SCAN_ORCA_ROUTE_LINE
    orca_optts_route_line: str = DEFAULT_SCAN_OPTTS_ORCA_ROUTE_LINE
    barrier_threshold_kcal: float = 0.5
    charge: int = 0
    multiplicity: int = 1
    scaffold_dir: str | Path | None = None


class NewCrestStageFactory(Protocol):
    def __call__(
        self,
        *,
        workflow_id: str,
        template_name: str,
        stage_id: str,
        source_path: str,
        input_role: str,
        mode: str,
        priority: int,
        max_cores: int,
        max_memory_gb: int,
        manifest_overrides: dict[str, Any] | None = None,
    ) -> WorkflowStageWithTaskPayload: ...


@dataclass(frozen=True)
class WorkflowCreationContext:
    workflow_id_factory: Callable[[], str]
    copy_input_fn: Callable[[str, Path], str]
    now_utc_iso_fn: Callable[[], str]
    new_crest_stage_fn: NewCrestStageFactory
    write_workflow_payload_fn: Callable[[Path, dict[str, Any]], Any]
    sync_workflow_registry_fn: Callable[[Path, Path, dict[str, Any]], Any]


@dataclass(frozen=True)
class ReactionTsSearchWorkflowCreationContext(WorkflowCreationContext):
    load_xyz_atom_sequence_fn: Callable[[str], Any]


@dataclass(frozen=True)
class WorkflowPersistenceContext:
    workflow_root_path: Path
    workspace_dir: Path
    workflow_id: str
    template_name: str
    source_job_id: str
    source_job_type: str
    reaction_key: str
    requested_at: str


__all__ = [
    "ConformerScreeningWorkflowRequest",
    "NewCrestStageFactory",
    "ReactionTsSearchWorkflowCreationContext",
    "ReactionTsSearchWorkflowRequest",
    "WorkflowCreationContext",
    "WorkflowPersistenceContext",
]
