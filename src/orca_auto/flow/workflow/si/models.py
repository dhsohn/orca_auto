"""Data models shared across workflow SI collection, science, rendering, and publication."""

from __future__ import annotations

from dataclasses import dataclass

from orca_auto.orca.report.interaction_energy import InteractionEnergyResult
from orca_auto.orca.report.rmsd import RmsdGroup
from orca_auto.orca.report.si import SiBlock


@dataclass(frozen=True)
class WorkflowSiEntry:
    stage_id: str
    block: SiBlock
    # The matched single-point block, kept so its level (method/basis/solvation/
    # version/route) can be documented: a composite energy is unreproducible
    # without the level that produced E(SP).
    sp_block: SiBlock | None = None
    sp_energy: float | None = None
    sp_label: str = ""
    composite_gibbs: float | None = None


@dataclass(frozen=True)
class ExcludedStage:
    stage_id: str
    label: str
    reason: str


@dataclass(frozen=True)
class PopulationRow:
    """One minimum's Boltzmann result within its ``formula|charge|multiplicity`` group.

    ``rel_e_kcalmol`` is relative to the group's lowest-electronic-energy member and
    ``rel_g_kcalmol`` to its lowest-Gibbs member (the population reference) — the same
    two-baseline convention as the relative-energy table; ``population`` is the
    within-group fraction (each group sums to 1).
    """

    cluster_key: str
    rel_e_kcalmol: float | None
    rel_g_kcalmol: float | None
    population: float | None


@dataclass(frozen=True)
class _EnergyConvention:
    use_single_point_energy: bool
    use_composite_gibbs: bool
    note: str = ""


@dataclass(frozen=True)
class WorkflowSiData:
    workflow_id: str
    template_name: str
    status: str
    reaction_key: str
    crest_conformer_total: int | None
    xtb_candidate_total: int | None
    entries: tuple[WorkflowSiEntry, ...]
    extra_blocks: tuple[WorkflowSiEntry, ...]
    excluded: tuple[ExcludedStage, ...]
    # Boltzmann populations aligned 1:1 with ``entries`` (empty tuple when the
    # workflow has no minima); ``None`` for a non-minimum or uncomputed entry.
    boltzmann_temperature_k: float | None = None
    boltzmann_temperature_source: str = ""
    population_note: str = ""
    populations: tuple[PopulationRow | None, ...] = ()
    # Interaction energies (ΔE_int) per retained representative complex, and the
    # RMSD re-dedup grouping applied to the minima. Both are empty/off unless the
    # respective manifest feature is enabled.
    interaction_energies: tuple[InteractionEnergyResult, ...] = ()
    interaction_energy_enabled: bool = False
    rmsd_dedup_enabled: bool = False
    rmsd_groups: tuple[RmsdGroup, ...] = ()

    def has_orca_stages(self) -> bool:
        return bool(self.entries or self.extra_blocks or self.excluded or self.interaction_energies)

    def rmsd_group_for(self, stage_id: str) -> tuple[int, RmsdGroup] | None:
        for index, group in enumerate(self.rmsd_groups, start=1):
            if stage_id in group.member_stage_ids:
                return index, group
        return None
