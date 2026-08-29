"""Workflow SI data models, durable evidence readers, science rules, and dataset assembly."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from orca_auto.core.utils.coercion import safe_int
from orca_auto.flow.contracts.workflow import (
    INTERACTION_COMPLEX_SP_ROLE,
    INTERACTION_CONFIG_FINGERPRINT_KEY,
    INTERACTION_FRAGMENT_ROLE,
    is_orca_stage_kind,
    is_supported_orca_stage_contract,
    is_valid_interaction_stage_contract,
    workflow_request_parameters,
    workflow_stage_dicts,
)
from orca_auto.flow.orca_stage_evidence import (
    collect_verified_orca_stage_evidence,
)
from orca_auto.flow.orca_stage_evidence import (
    stage_metadata as _stage_metadata,
)
from orca_auto.orca.parser import KCAL_PER_HARTREE
from orca_auto.orca.report.interaction_energy import (
    InteractionEnergyResult,
    InteractionFragmentEnergy,
    compute_interaction_energy,
    validate_fragment_electronic_states,
    validate_fragment_partition,
)
from orca_auto.orca.report.render import R_KCAL_PER_MOL_K
from orca_auto.orca.report.rmsd import RmsdGroup
from orca_auto.orca.report.si import SiBlock

from ...conformer_selection import (
    OrcaSelectedInputScienceIdentity,
    blocks_match_geometry,
    coordinates_match,
    eligible_minimum_block,
    finite,
    has_required_provenance,
    normalized_route_line,
    rmsd_candidate_for_block,
    rmsd_grouping,
    unique_single_point_matches,
)
from ...manifest import (
    interaction_energy_config_fingerprint,
    normalize_interaction_energy_block,
    normalize_rmsd_dedup_block,
    require_int,
    validate_conformer_postprocessing_template,
)
from ..stage_summary import (
    crest_stage_detail,
    stage_task_kind,
    xtb_stage_detail,
)

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return str(value or "").strip()


# ---------------------------------------------------------------------------
# Data models (shared across evidence, science, rendering, publication)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowSiEntry:
    stage_id: str
    block: SiBlock
    selected_input_identity: OrcaSelectedInputScienceIdentity | None = None
    # The matched single-point block, kept so its level (method/basis/solvation/
    # version/route) can be documented: a composite energy is unreproducible
    # without the level that produced E(SP).
    sp_block: SiBlock | None = None
    sp_selected_input_identity: OrcaSelectedInputScienceIdentity | None = None
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

    ``rel_g_kcalmol`` is relative to the group's lowest-Gibbs member, which is the
    population reference; ``population`` is the within-group fraction (each group
    sums to 1).
    """

    cluster_key: str
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
    rmsd_dedup_enabled: bool = False
    rmsd_groups: tuple[RmsdGroup, ...] = ()

    def has_orca_stages(self) -> bool:
        return bool(self.entries or self.extra_blocks or self.excluded or self.interaction_energies)


# ---------------------------------------------------------------------------
# Durable workflow-stage evidence readers
# ---------------------------------------------------------------------------


def _stage_label(stage: Mapping[str, Any]) -> str:
    return _text(_stage_metadata(stage).get("selected_input_label")) or _text(stage.get("stage_id"))


def _block_has_only_finite_numbers(block: SiBlock) -> bool:
    result = block.result
    optional_values = (
        result.energy_hartree,
        result.energy_ev,
        result.energy_kcalmol,
        result.lowest_freq_cm1,
        result.enthalpy,
        result.gibbs_energy,
        result.zpe_correction,
        result.gibbs_correction,
        result.thermo_temperature_k,
    )
    if any(value is not None and not math.isfinite(value) for value in optional_values):
        return False
    if any(not math.isfinite(value) for _, *coords in result.coordinates for value in coords):
        return False
    analysis = block.analysis
    if analysis is None:
        return True
    analysis_values = (
        *analysis.frequencies,
        *(value for columns in analysis.mode_matrix.values() for value in columns.values()),
        *(value for _, *coords in analysis.atoms for value in coords),
    )
    return all(math.isfinite(value) for value in analysis_values)


def _collect_stage_block(
    stage: Mapping[str, Any],
) -> tuple[SiBlock | None, str, OrcaSelectedInputScienceIdentity | None]:
    """Return a parsed block, exclusion reason, and bound selected-input identity."""
    block, reason, selected_input_identity = collect_verified_orca_stage_evidence(stage)
    if block is None:
        return None, reason, None
    if not _block_has_only_finite_numbers(block):
        return None, "output contains a non-finite numeric result", None
    return replace(block, name=_stage_label(stage)), "", selected_input_identity


# ---------------------------------------------------------------------------
# Scientific selection, deduplication, interaction, and population rules
# ---------------------------------------------------------------------------


# Boltzmann populations are physical only among interconverting minima of one
# species. This groups minima by formula/charge/multiplicity as a stoichiometric
# proxy for "one species" (it does not read connectivity, so constitutional
# isomers of the same formula pool together as an equilibrium over those minima)
# and requires a Gibbs free energy. Transition states and cross-group mixing are
# excluded, and populations are omitted (never fabricated) when the temperature is
# missing or ambiguous. ORCA prints THERMOCHEMISTRY AT to 2 decimals, so this
# tolerance only guards rounding and mixed-temperature runs.
_TEMP_TOLERANCE_K = 0.01
_TEMP_COMPARISON_EPSILON_K = 1e-9
_POP_NO_GIBBS_NOTE = (
    "(populations omitted: no complete set of converged, vibrationally verified "
    "minima with complete 3N spectra, finite Gibbs free energies, and "
    "thermochemistry temperatures)"
)


def _pair_single_points(
    stationary: list[WorkflowSiEntry],
    single_points: list[WorkflowSiEntry],
) -> tuple[list[WorkflowSiEntry], list[WorkflowSiEntry]]:
    """Pair only globally unique 1:1 geometry/electronic-state matches.

    The uniqueness rule (both 1:N and N:1 ambiguity pair nothing) lives in
    ``conformer_selection.unique_single_point_matches`` shared with the
    interaction-energy materializer.
    """
    unique = unique_single_point_matches(
        [entry.block for entry in stationary],
        [entry.block for entry in single_points],
    )
    paired: list[WorkflowSiEntry] = []
    used: set[int] = set()
    for entry, match_index in zip(stationary, unique, strict=True):
        correction = entry.block.result.gibbs_correction
        if match_index is None:
            paired.append(entry)
            continue
        match = single_points[match_index]
        used.add(match_index)
        sp_energy = match.block.result.energy_hartree
        assert sp_energy is not None  # guaranteed by the match predicate
        composite_sum = sp_energy + correction if correction is not None else None
        composite = composite_sum if finite(composite_sum) else None
        paired.append(
            replace(
                entry,
                sp_block=match.block,
                sp_selected_input_identity=match.selected_input_identity,
                sp_energy=sp_energy,
                sp_label=match.block.name,
                composite_gibbs=composite,
            )
        )
    return paired, [sp for index, sp in enumerate(single_points) if index not in used]


# ---------------------------------------------------------------------------
# RMSD re-dedup and interaction energies
# ---------------------------------------------------------------------------


def _meta_int_or_none(value: Any) -> int | None:
    """Strict metadata read: absent or non-integer is ``None``, never a guess.

    Mirrors ``require_int``: booleans and non-integer floats are corrupt
    metadata, not integers.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedup_minima(
    stationary: list[WorkflowSiEntry],
    cfg: Mapping[str, Any],
) -> tuple[list[WorkflowSiEntry], tuple[RmsdGroup, ...]]:
    """Collapse geometrically degenerate minima to their lowest-energy member.

    Only ``min`` entries are grouped (TS/SP are never merged). Non-representative
    minima are dropped from the returned list — which preserves the incoming
    energy sort — while the groups (including singletons) are returned so the SI
    can annotate each representative with its degeneracy.
    """
    mins = [entry for entry in stationary if _rmsd_eligible_minimum(entry)]
    if not mins:
        return stationary, ()
    if len(mins) == 1:
        only = mins[0].stage_id
        return stationary, (RmsdGroup(only, (only,)),)
    convention = _energy_convention(tuple(mins))
    candidates = [
        rmsd_candidate_for_block(
            entry.stage_id,
            entry.block,
            energy_hartree=(
                entry.sp_energy
                if convention.use_single_point_energy
                else entry.block.result.energy_hartree
            ),
            selected_input_identity=entry.selected_input_identity,
        )
        for entry in mins
    ]
    grouping = rmsd_grouping(candidates, cfg)
    representatives = grouping.representative_ids
    dropped = {c.stage_id for c in candidates if c.stage_id not in representatives}
    kept = [entry for entry in stationary if entry.stage_id not in dropped]
    return kept, grouping.groups


def _rmsd_eligible_minimum(
    entry: WorkflowSiEntry,
    *,
    expected_charge: int | None = None,
    expected_multiplicity: int | None = None,
) -> bool:
    """The materializer's fail-closed optimized-parent eligibility (shared rule)."""
    return eligible_minimum_block(
        entry.block,
        expected_charge=expected_charge,
        expected_multiplicity=expected_multiplicity,
    )


def _completed_interaction_block(stage: Mapping[str, Any]) -> tuple[SiBlock | None, str]:
    status = _text(stage.get("status"))
    if status != "completed":
        return None, f"stage status is {status or 'unknown'}"
    block, reason, _ = _collect_stage_block(stage)
    if block is None:
        return None, reason
    if block.kind != "sp" or block.analysis is not None:
        return None, "stage did not execute as a pure single point"
    return block, ""


def _interaction_config_fingerprint(
    cfg: dict[str, Any],
    parameters: Mapping[str, Any],
    rmsd_cfg: dict[str, Any] | None,
) -> str:
    complex_charge = require_int(parameters.get("charge", 0), field="charge")
    complex_multiplicity = require_int(
        parameters.get("multiplicity", 1), field="multiplicity", minimum=1
    )
    return interaction_energy_config_fingerprint(
        cfg,
        complex_charge=complex_charge,
        complex_multiplicity=complex_multiplicity,
        rmsd_dedup=rmsd_cfg,
    )


def _interaction_energy_results(
    interaction_stages: list[Mapping[str, Any]],
    stationary: list[WorkflowSiEntry],
    single_points: list[WorkflowSiEntry],
    cfg: dict[str, Any],
    parameters: Mapping[str, Any],
    rmsd_cfg: dict[str, Any] | None,
) -> tuple[InteractionEnergyResult, ...]:
    """Assemble ΔE_int per representative complex from its fan-out single points.

    Each interaction stage carries ``role`` (``interaction_complex_sp`` /
    ``interaction_fragment``) and ``parent_stage_id`` linking it to the complex
    optimization it was fanned out from. The complex and fragment energies come
    from the same-level single points; a missing one makes that complex's ΔE_int
    a fail-closed omission (never a partial sum).
    """
    expected_fragments = cfg.get("fragments")
    if not isinstance(expected_fragments, list):
        return ()
    complex_charge = require_int(parameters.get("charge", 0), field="charge")
    complex_multiplicity = require_int(
        parameters.get("multiplicity", 1), field="multiplicity", minimum=1
    )
    eligible = [
        entry
        for entry in stationary
        if _rmsd_eligible_minimum(
            entry,
            expected_charge=complex_charge,
            expected_multiplicity=complex_multiplicity,
        )
    ]
    eligible, _unused_single_points = _pair_single_points(eligible, single_points)
    entry_by_stage = {entry.stage_id: entry for entry in eligible}
    stages_by_parent: dict[str, list[Mapping[str, Any]]] = {}
    for stage in interaction_stages:
        meta = _stage_metadata(stage)
        parent = _text(meta.get("parent_stage_id"))
        if not parent or parent not in entry_by_stage:
            continue
        stages_by_parent.setdefault(parent, []).append(stage)
    expected_fingerprint = _interaction_config_fingerprint(cfg, parameters, rmsd_cfg)
    # ``rmsd_grouping`` applies the shared defaults when the block is absent,
    # exactly as the materializer does for its fan-out decision.
    expected_ranked, _hidden_groups = _dedup_minima(eligible, rmsd_cfg or {})
    expected_parent_ids = {entry.stage_id for entry in expected_ranked}
    expected_route = normalized_route_line(cfg.get("sp_route_line"))
    results: list[InteractionEnergyResult] = []
    for parent in (entry.stage_id for entry in eligible if entry.stage_id in expected_parent_ids):
        entry = entry_by_stage[parent]
        parent_stages = stages_by_parent.get(parent, [])
        blockers: list[str] = []
        complex_candidates = [
            stage
            for stage in parent_stages
            if _text(_stage_metadata(stage).get("role")) == INTERACTION_COMPLEX_SP_ROLE
        ]
        fragment_candidates: dict[int, list[Mapping[str, Any]]] = {}
        for stage in parent_stages:
            meta = _stage_metadata(stage)
            role = _text(meta.get("role"))
            if role == INTERACTION_FRAGMENT_ROLE:
                index = safe_int(meta.get("fragment_index"), default=-1)
                fragment_candidates.setdefault(index, []).append(stage)
            elif role != INTERACTION_COMPLEX_SP_ROLE:
                blockers.append(f"unexpected interaction stage role {role or 'missing'}")

        if len(complex_candidates) != 1:
            blockers.append(
                f"expected exactly one complex single point, found {len(complex_candidates)}"
            )
        unexpected_indices = sorted(set(fragment_candidates) - set(range(len(expected_fragments))))
        if unexpected_indices:
            blockers.append(f"unexpected fragment indices {unexpected_indices}")

        opt_coordinates = tuple(entry.block.result.coordinates)
        partition_reason = validate_fragment_partition(
            [fragment["atom_indices"] for fragment in expected_fragments],
            len(opt_coordinates),
        )
        if partition_reason:
            blockers.append(partition_reason)
        state_reason = validate_fragment_electronic_states(
            [row[0] for row in opt_coordinates], expected_fragments
        )
        if state_reason:
            blockers.append(state_reason)

        observed_blocks: list[SiBlock] = []
        complex_block: SiBlock | None = None
        complex_sp_stage_id = ""
        if len(complex_candidates) == 1:
            complex_stage = complex_candidates[0]
            complex_sp_stage_id = _text(complex_stage.get("stage_id"))
            meta = _stage_metadata(complex_stage)
            if _text(meta.get(INTERACTION_CONFIG_FINGERPRINT_KEY)) != expected_fingerprint:
                blockers.append("complex stage belongs to another interaction config generation")
            complex_block, reason = _completed_interaction_block(complex_stage)
            if complex_block is None:
                blockers.append(f"complex single point unavailable: {reason}")
            else:
                observed_blocks.append(complex_block)
                if not blocks_match_geometry(entry.block, complex_block):
                    blockers.append(
                        "complex single point does not use the optimized complex geometry"
                    )
                if (
                    not complex_block.result.electronic_state_verified
                    or complex_block.result.charge != complex_charge
                    or complex_block.result.multiplicity != complex_multiplicity
                ):
                    blockers.append(
                        "complex single-point selected-input route/electronic state does not "
                        "match the request"
                    )

        fragment_rows: list[InteractionFragmentEnergy] = []
        for index, expected in enumerate(expected_fragments):
            candidates = fragment_candidates.get(index, [])
            expected_indices = tuple(int(value) for value in expected["atom_indices"])
            expected_charge = int(expected["charge"])
            expected_multiplicity = int(expected["multiplicity"])
            expected_label = _text(expected.get("label")) or f"fragment_{index + 1}"
            block: SiBlock | None = None
            stage_id = ""
            if len(candidates) != 1:
                blockers.append(
                    f"fragment {index} expected exactly one stage, found {len(candidates)}"
                )
            else:
                stage = candidates[0]
                stage_id = _text(stage.get("stage_id"))
                meta = _stage_metadata(stage)
                if _text(meta.get(INTERACTION_CONFIG_FINGERPRINT_KEY)) != expected_fingerprint:
                    blockers.append(f"fragment {index} belongs to another config generation")
                if tuple(meta.get("fragment_atom_indices", ())) != expected_indices:
                    blockers.append(
                        f"fragment {index} atom-index metadata differs from the request"
                    )
                # Absent or corrupt metadata must never pass by defaulting to the
                # expected value: the gate exists to catch exactly that drift.
                if (
                    _meta_int_or_none(meta.get("fragment_charge")) != expected_charge
                    or _meta_int_or_none(meta.get("fragment_multiplicity")) != expected_multiplicity
                ):
                    blockers.append(
                        f"fragment {index} electronic-state metadata differs or is missing"
                    )
                block, reason = _completed_interaction_block(stage)
                if block is None:
                    blockers.append(f"fragment {index} single point unavailable: {reason}")
                else:
                    observed_blocks.append(block)
                    expected_coordinates = tuple(
                        opt_coordinates[position] for position in expected_indices
                    )
                    if not coordinates_match(expected_coordinates, tuple(block.result.coordinates)):
                        blockers.append(
                            f"fragment {index} geometry is not the requested complex subset"
                        )
                    if (
                        not block.result.electronic_state_verified
                        or block.result.charge != expected_charge
                        or block.result.multiplicity != expected_multiplicity
                    ):
                        blockers.append(
                            f"fragment {index} selected-input route/electronic state does not "
                            "match the request"
                        )
            fragment_rows.append(
                InteractionFragmentEnergy(
                    label=expected_label,
                    stage_id=stage_id,
                    charge=expected_charge,
                    multiplicity=expected_multiplicity,
                    energy_hartree=block.result.energy_hartree if block is not None else None,
                    atom_indices=expected_indices,
                    formula=block.result.formula if block is not None else "",
                )
            )

        provenance: tuple[str, str, str, str, str] = ("", "", "", "", "")
        if len(observed_blocks) != 1 + len(expected_fragments):
            blockers.append("the complete complex/fragment single-point set is not available")
        elif any(not has_required_provenance(block) for block in observed_blocks):
            blockers.append("single-point provenance is incomplete")
        else:
            levels = {
                (
                    block.result.method,
                    block.result.basis_set,
                    block.result.solvation,
                    block.result.orca_version,
                    normalized_route_line(block.result.input_line),
                )
                for block in observed_blocks
            }
            if len(levels) != 1:
                blockers.append("complex and fragment single-point levels differ")
            else:
                provenance = next(iter(levels))
                if provenance[4] != expected_route:
                    blockers.append(
                        "executed single-point route differs from interaction_energy.sp_route_line"
                    )

        results.append(
            compute_interaction_energy(
                complex_stage_id=complex_sp_stage_id,
                complex_label=entry.block.name,
                complex_charge=complex_charge,
                complex_multiplicity=complex_multiplicity,
                complex_energy_hartree=(
                    complex_block.result.energy_hartree if complex_block is not None else None
                ),
                fragments=fragment_rows,
                blocker="; ".join(dict.fromkeys(blockers)),
                complex_formula=(complex_block.result.formula if complex_block is not None else ""),
                method=provenance[0],
                basis_set=provenance[1],
                solvation=provenance[2],
                orca_version=provenance[3],
                input_line=(complex_block.result.input_line if complex_block is not None else ""),
                parent_stage_id=parent,
            )
        )
    return tuple(results)


# ---------------------------------------------------------------------------
# Boltzmann populations
# ---------------------------------------------------------------------------


def _cluster_key(entry: WorkflowSiEntry) -> str:
    result = entry.block.result
    return f"{result.formula or '?'}|{result.charge}|{result.multiplicity}"


def _provenance_key(
    block: SiBlock,
    selected_input_identity: OrcaSelectedInputScienceIdentity,
) -> tuple[str, str, str, str, OrcaSelectedInputScienceIdentity]:
    """Exact executed level and electronic state used for cross-structure comparisons."""
    result = block.result
    return (
        result.method,
        result.basis_set,
        result.solvation,
        result.orca_version,
        selected_input_identity,
    )


def _has_complete_comparison_provenance(
    block: SiBlock | None,
    selected_input_identity: OrcaSelectedInputScienceIdentity | None,
) -> bool:
    return (
        block is not None and selected_input_identity is not None and has_required_provenance(block)
    )


def _has_complete_vibrational_spectrum(entry: WorkflowSiEntry) -> bool:
    analysis = entry.block.analysis
    atom_count = len(entry.block.result.coordinates)
    return analysis is not None and atom_count > 0 and len(analysis.frequencies) == 3 * atom_count


def _temperatures_agree(left: float, right: float) -> bool:
    # Parsed ORCA temperatures have two decimals. A tiny arithmetic epsilon
    # keeps the documented inclusive 0.01 K boundary stable for values such as
    # 1.00 and 1.01 without widening the scientific tolerance materially.
    return abs(left - right) <= _TEMP_TOLERANCE_K + _TEMP_COMPARISON_EPSILON_K


def _energy_convention(entries: tuple[WorkflowSiEntry, ...]) -> _EnergyConvention:
    """One effective E/G convention shared by the relative and population tables."""
    candidates = [entry for entry in entries if finite(entry.block.result.energy_hartree)]
    if not candidates or not any(entry.sp_energy is not None for entry in candidates):
        return _EnergyConvention(False, False)

    all_refined = all(
        entry.sp_block is not None and finite(entry.sp_energy) for entry in candidates
    )
    if not all_refined:
        return _EnergyConvention(
            False,
            False,
            "single-point refinements cover only part of the stationary set; "
            "optimization-level E and G are used throughout",
        )

    if not all(
        _has_complete_comparison_provenance(
            entry.sp_block,
            entry.sp_selected_input_identity,
        )
        for entry in candidates
    ):
        return _EnergyConvention(
            False,
            False,
            "single-point provenance is incomplete; optimization-level E and G are used throughout",
        )

    sp_levels = {
        _provenance_key(entry.sp_block, entry.sp_selected_input_identity)
        for entry in candidates
        if entry.sp_block is not None and entry.sp_selected_input_identity is not None
    }
    if len(sp_levels) != 1:
        return _EnergyConvention(
            False,
            False,
            "single-point refinement levels differ; optimization-level E and G are used throughout",
        )

    composite_ready = all(finite(entry.composite_gibbs) for entry in candidates)
    opt_levels = {
        _provenance_key(entry.block, entry.selected_input_identity)
        for entry in candidates
        if entry.selected_input_identity is not None
    }
    if not composite_ready:
        return _EnergyConvention(
            True,
            False,
            "single-point E is used, but G remains at the optimization level because "
            "thermochemical corrections are incomplete",
        )
    if not all(
        _has_complete_comparison_provenance(entry.block, entry.selected_input_identity)
        for entry in candidates
    ):
        return _EnergyConvention(
            True,
            False,
            "single-point E is used, but G remains at the optimization level because "
            "optimization/frequency provenance is incomplete",
        )
    if len(opt_levels) != 1:
        return _EnergyConvention(
            True,
            False,
            "single-point E is used, but G remains at the optimization level because "
            "optimization/frequency levels differ",
        )
    return _EnergyConvention(True, True)


def _compute_populations(
    entries: tuple[WorkflowSiEntry, ...],
    override: float | None,
    *,
    blocker: str = "",
) -> tuple[tuple[PopulationRow | None, ...], float | None, str, str]:
    """Per-species Boltzmann populations over minima; fail closed to omission.

    Returns ``(rows, temperature, source, note)``. ``rows`` aligns 1:1 with
    ``entries``; it is an empty tuple when the workflow has no minima at all (the
    section is suppressed), and a length-matched tuple of mostly ``None`` when
    minima exist but populations cannot be computed (the section shows ``note``).
    Every route-classified minimum must be converged, have ``Nimag == 0``, and
    carry finite Gibbs/temperature data. A subset is never renormalized to 100%.
    """
    blank: tuple[PopulationRow | None, ...] = tuple(None for _ in entries)
    if blocker:
        return blank, None, "", blocker

    min_indices = [i for i, entry in enumerate(entries) if entry.block.kind == "min"]
    if not min_indices:
        return (), None, "", ""

    usable = [
        i
        for i in min_indices
        if entries[i].block.result.opt_converged is True
        and entries[i].block.imaginary_count == 0
        and _has_complete_vibrational_spectrum(entries[i])
        and finite(entries[i].block.result.energy_hartree)
        and finite(entries[i].block.result.gibbs_energy)
        and finite(entries[i].block.result.thermo_temperature_k)
        and (entries[i].block.result.thermo_temperature_k or 0.0) > 0
    ]
    if len(usable) != len(min_indices):
        return (
            blank,
            None,
            "",
            f"{_POP_NO_GIBBS_NOTE[:-1]}; {len(usable)} of {len(min_indices)} "
            "route-classified minima are usable)",
        )

    clusters: dict[str, list[int]] = {}
    for i in usable:
        clusters.setdefault(_cluster_key(entries[i]), []).append(i)
    for key, members in clusters.items():
        if (
            not all(
                _has_complete_comparison_provenance(
                    entries[i].block,
                    entries[i].selected_input_identity,
                )
                for i in members
            )
            or len(
                {
                    _provenance_key(entries[i].block, selected_input_identity)
                    for i in members
                    if (selected_input_identity := entries[i].selected_input_identity) is not None
                }
            )
            != 1
        ):
            return (
                blank,
                None,
                "",
                "(populations omitted: optimization/frequency provenance is missing or "
                "differs within "
                f"formula|charge|multiplicity group {key})",
            )

    temps = [t for i in usable if (t := entries[i].block.result.thermo_temperature_k) is not None]
    t_low, t_high = min(temps), max(temps)
    # Pooled minima must share one frequency temperature: a Gibbs energy embeds
    # −T·S at the job's own T, so mixing temperatures is unphysical no matter which
    # temperature the weighting uses. The manifest key only pins/labels that shared
    # temperature — it cannot reconcile genuinely disagreeing frequency jobs.
    if not _temperatures_agree(t_low, t_high):
        return (
            blank,
            None,
            "",
            "(populations omitted: thermochemistry temperatures disagree — "
            f"{t_low:.2f}–{t_high:.2f} K; populations need one frequency temperature)",
        )
    if override is not None:
        if isinstance(override, bool) or not math.isfinite(override) or override <= 0:
            return blank, None, "", "(populations omitted: invalid boltzmann_temperature_k)"
        if not _temperatures_agree(override, t_low) or not _temperatures_agree(override, t_high):
            return (
                blank,
                None,
                "",
                f"(populations omitted: manifest boltzmann_temperature_k = {override:.2f} K "
                f"disagrees with the thermochemistry temperature {t_low:.2f}–{t_high:.2f} K)",
            )
        temperature = override
        source = "manifest boltzmann_temperature_k"
    else:
        temperature = temps[0]
        source = "thermochemistry output"

    rows: list[PopulationRow | None] = [None] * len(entries)
    rt = R_KCAL_PER_MOL_K * temperature
    if not math.isfinite(rt) or rt <= 0:
        return blank, None, "", "(populations omitted: invalid finite R·T weighting scale)"
    convention = _energy_convention(entries)
    for key, members in clusters.items():
        gibbs: dict[int, float] = {}
        energy: dict[int, float | None] = {}
        for i in members:
            entry = entries[i]
            value = (
                entry.composite_gibbs
                if convention.use_composite_gibbs
                else entry.block.result.gibbs_energy
            )
            assert value is not None  # complete-data/convention gates guarantee this
            gibbs[i] = value
            energy[i] = (
                entry.sp_energy
                if convention.use_single_point_energy
                else entry.block.result.energy_hartree
            )
        g_min = min(gibbs.values())
        present_energy = [e for e in energy.values() if e is not None]
        e_min = min(present_energy) if present_energy else None
        rel_g = {i: (gibbs[i] - g_min) * KCAL_PER_HARTREE for i in members}
        rel_e: dict[int, float | None] = {}
        for i in members:
            energy_i = energy[i]
            rel_e[i] = (
                (energy_i - e_min) * KCAL_PER_HARTREE
                if energy_i is not None and e_min is not None
                else None
            )
        if any(not math.isfinite(value) for value in rel_g.values()) or any(
            value is not None and not math.isfinite(value) for value in rel_e.values()
        ):
            return (
                blank,
                None,
                "",
                "(populations omitted: relative energies exceed the finite numeric range)",
            )
        weights = {i: math.exp(-(rel_g[i] / rt)) for i in members}
        partition = sum(weights.values())
        if not math.isfinite(partition) or partition <= 0:
            return blank, None, "", "(populations omitted: invalid Boltzmann partition sum)"
        for i in members:
            rows[i] = PopulationRow(
                cluster_key=key,
                rel_g_kcalmol=rel_g[i],
                population=weights[i] / partition,
            )

    note = f"⚠ {convention.note}" if convention.note else ""
    return tuple(rows), temperature, source, note


# ---------------------------------------------------------------------------
# Assemble durable evidence into one scientifically interpreted SI dataset
# ---------------------------------------------------------------------------


def collect_workflow_si_data(
    payload: Mapping[str, Any],
    *,
    boltzmann_temperature_k: float | None = None,
    population_blocker: str = "",
) -> WorkflowSiData:
    template_name = _text(payload.get("template_name"))
    workflow_status = _text(payload.get("status"))
    parameters = workflow_request_parameters(payload)
    try:
        interaction_cfg = normalize_interaction_energy_block(parameters.get("interaction_energy"))
    except ValueError:
        logger.warning("Invalid durable interaction_energy configuration", exc_info=True)
        interaction_cfg = None
    try:
        rmsd_cfg = normalize_rmsd_dedup_block(parameters.get("rmsd_dedup"))
    except ValueError:
        logger.warning("Invalid durable rmsd_dedup configuration", exc_info=True)
        rmsd_cfg = None
    try:
        validate_conformer_postprocessing_template(
            template_name,
            interaction_energy=interaction_cfg,
            rmsd_dedup=rmsd_cfg,
        )
    except ValueError:
        logger.warning("Conformer post-processing is disabled for this template", exc_info=True)
        interaction_cfg = None
        rmsd_cfg = None
    expected_interaction_fingerprint = ""
    if interaction_cfg is not None:
        try:
            expected_interaction_fingerprint = _interaction_config_fingerprint(
                interaction_cfg,
                parameters,
                rmsd_cfg,
            )
        except ValueError:
            # The requested feature will fail explicitly during assembly. Until
            # then, no stage may be hidden as a trusted generated child.
            pass
    crest_total: int | None = None
    xtb_total: int | None = None
    stationary: list[WorkflowSiEntry] = []
    single_points: list[WorkflowSiEntry] = []
    extra: list[WorkflowSiEntry] = []
    excluded: list[ExcludedStage] = []
    incomplete_population_stages: list[str] = []
    # Interaction-energy fragment/complex single points are internal inputs, not
    # SI structures. Only stages satisfying the complete interaction-stage
    # contract are pulled out BEFORE any min/ts/sp classification, so arbitrary
    # role metadata cannot hide a primary result from the structures list.
    interaction_raw_stages: list[Mapping[str, Any]] = []
    stages = workflow_stage_dicts(payload)

    for stage in stages:
        stage_kind = _text(stage.get("stage_kind"))
        if stage_kind == "crest_stage":
            _, frames = crest_stage_detail(stage)
            if frames is not None:
                crest_total = (crest_total or 0) + frames
            continue
        if stage_kind == "xtb_stage":
            _, candidates = xtb_stage_detail(stage)
            xtb_total = (xtb_total or 0) + candidates
            continue
        if not is_orca_stage_kind(stage):
            continue
        if not is_supported_orca_stage_contract(stage):
            stage_id = _text(stage.get("stage_id"))
            label = _stage_label(stage)
            excluded.append(
                ExcludedStage(
                    stage_id,
                    label,
                    "unsupported or contradictory ORCA stage contract",
                )
            )
            if template_name == "conformer_screening":
                incomplete_population_stages.append(label or stage_id or "unknown")
            continue
        if is_valid_interaction_stage_contract(
            stage,
            stages,
            expected_config_fingerprint=expected_interaction_fingerprint,
        ):
            interaction_raw_stages.append(stage)
            continue

        stage_id = _text(stage.get("stage_id"))
        label = _stage_label(stage)
        status = _text(stage.get("status"))
        if stage_task_kind(stage) == "relaxed_scan":
            excluded.append(
                ExcludedStage(
                    stage_id, label, "relaxed scan (prerequisite, not a stationary point)"
                )
            )
            continue
        if status != "completed":
            excluded.append(ExcludedStage(stage_id, label, f"stage status: {status or 'unknown'}"))
            if template_name == "conformer_screening":
                incomplete_population_stages.append(label or stage_id or "unknown")
            continue
        block, reason, selected_input_identity = _collect_stage_block(stage)
        if block is None:
            excluded.append(ExcludedStage(stage_id, label, reason))
            if template_name == "conformer_screening":
                incomplete_population_stages.append(label or stage_id or "unknown")
            continue
        if (
            template_name == "conformer_screening"
            and block.kind != "min"
            and (stage_task_kind(stage) == "opt" or block.kind == "ts")
        ):
            incomplete_population_stages.append(label or stage_id or "unknown")
        entry = WorkflowSiEntry(
            stage_id=stage_id,
            block=block,
            selected_input_identity=selected_input_identity,
        )
        if block.kind in ("min", "ts"):
            stationary.append(entry)
        elif block.analysis is None:
            single_points.append(entry)
        else:
            extra.append(entry)

    stationary.sort(
        key=lambda entry: (
            entry.block.result.energy_hartree is None,
            entry.block.result.energy_hartree or 0.0,
        )
    )
    # Give scientifically eligible minima first claim on optional SP refinements.
    # A known saddle/unconverged structure at the same geometry must not make an
    # otherwise unique minimum refinement ambiguous. Remaining stationary
    # structures may pair only with SPs left after that canonical pass.
    eligible_stationary = [entry for entry in stationary if _rmsd_eligible_minimum(entry)]
    eligible_paired, remaining_single_points = _pair_single_points(
        eligible_stationary, single_points
    )
    eligible_blocks = {id(entry.block) for entry in eligible_stationary}
    ineligible_paired, pre_dedup_unpaired = _pair_single_points(
        [entry for entry in stationary if id(entry.block) not in eligible_blocks],
        remaining_single_points,
    )
    # Corrupt payloads may repeat stage IDs. SiBlock identity is stable through
    # dataclass replacement and keeps this merge 1:1.
    paired_by_block = {id(entry.block): entry for entry in (*eligible_paired, *ineligible_paired)}
    pre_dedup_ranked = [paired_by_block.get(id(entry.block), entry) for entry in stationary]

    # Validate population completeness against the full pre-dedup ensemble.
    # Dropping an unusable duplicate must never turn an incomplete ensemble into
    # a fabricated 100% population for the remaining representative.
    try:
        if not population_blocker and template_name == "conformer_screening":
            if workflow_status != "completed":
                population_blocker = (
                    "(populations omitted: the conformer ensemble is not terminal; "
                    f"workflow status is {workflow_status or 'unknown'})"
                )
            elif incomplete_population_stages:
                population_blocker = (
                    "(populations omitted: the conformer ensemble is incomplete; "
                    f"{len(incomplete_population_stages)} ORCA stage(s) are not usable)"
                )
        if not population_blocker and template_name == "conformer_screening":
            pre_rows, _pre_t, _pre_source, pre_note = _compute_populations(
                tuple(pre_dedup_ranked), boltzmann_temperature_k
            )
            pre_min_indices = [
                index for index, entry in enumerate(pre_dedup_ranked) if entry.block.kind == "min"
            ]
            if pre_min_indices and any(pre_rows[index] is None for index in pre_min_indices):
                population_blocker = pre_note or "(populations omitted: pre-dedup ensemble invalid)"
    except Exception:  # noqa: BLE001
        logger.warning("Pre-dedup population validation failed", exc_info=True)
        population_blocker = "(populations omitted: population validation failed)"

    # RMSD re-dedup and interaction-energy assembly are requested explicitly, so
    # a failure in either aborts the whole publication rather than quietly
    # dropping the feature: an SI that silently omits a section the workflow
    # asked for reads as complete and is not. The last known-good SI stays on
    # disk and the durable publication state machine retries or blocks.
    rmsd_groups: tuple[RmsdGroup, ...] = ()
    ranked = pre_dedup_ranked
    if rmsd_cfg is not None:
        ranked, rmsd_groups = _dedup_minima(pre_dedup_ranked, rmsd_cfg)
    unpaired = pre_dedup_unpaired

    interaction_energies: tuple[InteractionEnergyResult, ...] = ()
    if interaction_cfg is not None:
        interaction_energies = _interaction_energy_results(
            interaction_raw_stages,
            stationary,
            single_points,
            interaction_cfg,
            parameters,
            rmsd_cfg,
        )

    # A population bug must never replace a valid SI with stale files: isolate the
    # computation so the base document (methods, relative-energy table, structures)
    # still renders even if this raises.
    try:
        populations, temperature, temperature_source, population_note = _compute_populations(
            tuple(ranked), boltzmann_temperature_k, blocker=population_blocker
        )
    except Exception:  # noqa: BLE001
        logger.warning("Boltzmann population computation failed", exc_info=True)
        populations, temperature, temperature_source, population_note = (
            tuple(None for _ in ranked),
            None,
            "",
            "(populations omitted: population computation failed; inspect the application log)",
        )

    return WorkflowSiData(
        workflow_id=_text(payload.get("workflow_id")),
        template_name=template_name,
        status=workflow_status,
        reaction_key=_text(payload.get("reaction_key")),
        crest_conformer_total=crest_total,
        xtb_candidate_total=xtb_total,
        entries=tuple(ranked),
        extra_blocks=tuple(unpaired + extra),
        excluded=tuple(excluded),
        boltzmann_temperature_k=temperature,
        boltzmann_temperature_source=temperature_source,
        population_note=population_note,
        populations=populations,
        interaction_energies=interaction_energies,
        rmsd_dedup_enabled=rmsd_cfg is not None,
        rmsd_groups=rmsd_groups,
    )
