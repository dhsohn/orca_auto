"""Scientific selection, deduplication, interaction, and population rules for workflow SI."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

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
from ...manifest import interaction_energy_config_fingerprint, require_int
from ..report import _stage_metadata, _text
from .evidence import _collect_stage_block
from .models import PopulationRow, WorkflowSiEntry, _EnergyConvention

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

# Metadata role prefix carried by the interaction-energy single-point stages the
# orchestration fans out (``interaction_complex_sp`` and ``interaction_fragment``).
_INTERACTION_ROLE_PREFIX = "interaction_"
_INTERACTION_CONFIG_FINGERPRINT_KEY = "interaction_config_fingerprint"
_ROLE_INTERACTION_COMPLEX = "interaction_complex_sp"
_ROLE_INTERACTION_FRAGMENT = "interaction_fragment"


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
                sp_energy=sp_energy,
                sp_label=match.block.name,
                composite_gibbs=composite,
            )
        )
    return paired, [sp for index, sp in enumerate(single_points) if index not in used]


# ---------------------------------------------------------------------------
# RMSD re-dedup and interaction energies
# ---------------------------------------------------------------------------


def _meta_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
    block, reason = _collect_stage_block(stage)
    if block is None:
        return None, reason
    if block.kind != "sp" or block.analysis is not None:
        return None, "stage did not execute as a pure single point"
    return block, ""


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
    expected_fingerprint = interaction_energy_config_fingerprint(
        cfg,
        complex_charge=complex_charge,
        complex_multiplicity=complex_multiplicity,
        rmsd_dedup=rmsd_cfg,
    )
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
            if _text(_stage_metadata(stage).get("role")) == _ROLE_INTERACTION_COMPLEX
        ]
        fragment_candidates: dict[int, list[Mapping[str, Any]]] = {}
        for stage in parent_stages:
            meta = _stage_metadata(stage)
            role = _text(meta.get("role"))
            if role == _ROLE_INTERACTION_FRAGMENT:
                index = _meta_int(meta.get("fragment_index"), -1)
                fragment_candidates.setdefault(index, []).append(stage)
            elif role != _ROLE_INTERACTION_COMPLEX:
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
            if _text(meta.get(_INTERACTION_CONFIG_FINGERPRINT_KEY)) != expected_fingerprint:
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
                if _text(meta.get(_INTERACTION_CONFIG_FINGERPRINT_KEY)) != expected_fingerprint:
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


def _provenance_key(block: SiBlock) -> tuple[str, str, str, str, str, int, int]:
    """Exact executed level and electronic state used for cross-structure comparisons."""
    result = block.result
    return (
        result.method,
        result.basis_set,
        result.solvation,
        result.orca_version,
        result.input_line,
        result.charge,
        result.multiplicity,
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
        entry.sp_block is not None and has_required_provenance(entry.sp_block)
        for entry in candidates
    ):
        return _EnergyConvention(
            False,
            False,
            "single-point provenance is incomplete; optimization-level E and G are used throughout",
        )

    sp_levels = {_provenance_key(entry.sp_block) for entry in candidates if entry.sp_block}
    if len(sp_levels) != 1:
        return _EnergyConvention(
            False,
            False,
            "single-point refinement levels differ; optimization-level E and G are used throughout",
        )

    composite_ready = all(finite(entry.composite_gibbs) for entry in candidates)
    opt_levels = {_provenance_key(entry.block) for entry in candidates}
    if not composite_ready:
        return _EnergyConvention(
            True,
            False,
            "single-point E is used, but G remains at the optimization level because "
            "thermochemical corrections are incomplete",
        )
    if not all(has_required_provenance(entry.block) for entry in candidates):
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
            not all(has_required_provenance(entries[i].block) for i in members)
            or len({_provenance_key(entries[i].block) for i in members}) != 1
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
                rel_e_kcalmol=rel_e[i],
                rel_g_kcalmol=rel_g[i],
                population=weights[i] / partition,
            )

    note = f"⚠ {convention.note}" if convention.note else ""
    return tuple(rows), temperature, source, note
