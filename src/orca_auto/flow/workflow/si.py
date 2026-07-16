"""Copy-paste-ready Supporting Information for a whole workflow run.

``workflow_si.md`` assembles what a paper's SI needs from every completed
ORCA stage: a computational-details paragraph generated from the routes and
program versions that actually ran (not from memory), the CREST → xTB → ORCA
funnel provenance, a relative-energy table, and each structure's SI block.
``si_data.csv`` carries the same numbers in machine-readable form for
data-availability requirements.

When an opt+freq structure has one globally unique single-point match
(identical geometry within 1e-4 Å and the same charge/multiplicity), the table
adds a composite G = E(SP) + [G − E(el)](opt level). Ambiguous or wrong-state
single points stay explicit instead of silently refining the wrong structure.
Rewritten on every workflow advance and assembled from completed stages only,
so a partial workflow yields a partial (still valid) SI.
Generation must never break the advance: errors are logged and swallowed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import (
    INTERACTION_ENERGY_CSV_FILE,
    INTERACTION_ENERGY_CSV_OWNER_FILE,
    WORKFLOW_SI_CSV_FILE,
    WORKFLOW_SI_MD_FILE,
)
from orca_auto.core.utils.persistence import atomic_write_text
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
from orca_auto.orca.report.si import (
    SiBlock,
    SiBlockError,
    collect_si_block,
    render_si_block_md,
)
from orca_auto.orca.state import load_state

from ..conformer_selection import (
    blocks_match_geometry,
    coordinates_match,
    eligible_minimum_block,
    finite,
    has_required_provenance,
    normalized_route_line,
    rmsd_candidate_for_block,
    rmsd_grouping,
    selected_input_state_matches,
    unique_single_point_matches,
)
from ..manifest import (
    interaction_energy_config_fingerprint,
    normalize_interaction_energy_block,
    normalize_rmsd_dedup_block,
    optional_positive_float,
    require_int,
    validate_conformer_postprocessing_template,
    validate_interaction_energy_state_balance,
)
from .report import (
    _crest_stage_detail,
    _orca_stage_output_dir,
    _stage_dicts,
    _stage_metadata,
    _task_kind,
    _text,
    _xtb_stage_detail,
)

logger = logging.getLogger(__name__)

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
) -> tuple[SiBlock | None, str]:
    """(block, exclusion_reason) — exactly one side is meaningful."""
    reaction_dir = _orca_stage_output_dir(stage)
    if reaction_dir is None:
        return None, "no output directory recorded"
    state = load_state(reaction_dir)
    if state is None:
        return None, "no job state found"
    try:
        block = collect_si_block(reaction_dir, state)
    except SiBlockError as exc:
        return None, str(exc)
    if block is None:
        return None, "job type has no SI block"
    if not _block_has_only_finite_numbers(block):
        return None, "output contains a non-finite numeric result"
    result = block.result
    state_verified = result.electronic_state_verified and selected_input_state_matches(block, state)
    if not state_verified:
        warning = "route/electronic-state provenance missing or inconsistent with selected input"
        block = replace(
            block,
            result=replace(result, electronic_state_verified=False),
            warnings=(*block.warnings, warning),
        )
    return replace(block, name=_stage_label(stage)), ""


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


def collect_workflow_si_data(
    payload: Mapping[str, Any],
    *,
    boltzmann_temperature_k: float | None = None,
    population_blocker: str = "",
    raise_feature_errors: bool = False,
) -> WorkflowSiData:
    template_name = _text(payload.get("template_name"))
    workflow_status = _text(payload.get("status"))
    parameters = _request_parameters(payload)
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
    crest_total: int | None = None
    xtb_total: int | None = None
    stationary: list[WorkflowSiEntry] = []
    single_points: list[WorkflowSiEntry] = []
    extra: list[WorkflowSiEntry] = []
    excluded: list[ExcludedStage] = []
    incomplete_population_stages: list[str] = []
    # Interaction-energy fragment/complex single points are internal inputs, not
    # SI structures: they carry a ``role`` starting ``interaction_`` and must be
    # pulled out BEFORE any min/ts/sp classification so they can never leak into
    # the relative-energy table, the structures list, or si_data.csv, nor be
    # folded into a stationary structure by ``_pair_single_points``.
    interaction_raw_stages: list[Mapping[str, Any]] = []

    for stage in _stage_dicts(payload):
        stage_kind = _text(stage.get("stage_kind"))
        if stage_kind == "crest_stage":
            _, frames = _crest_stage_detail(stage)
            if frames is not None:
                crest_total = (crest_total or 0) + frames
            continue
        if stage_kind == "xtb_stage":
            _, candidates = _xtb_stage_detail(stage)
            xtb_total = (xtb_total or 0) + candidates
            continue
        if stage_kind != "orca_stage":
            continue
        if _text(_stage_metadata(stage).get("role")).startswith(_INTERACTION_ROLE_PREFIX):
            interaction_raw_stages.append(stage)
            continue

        stage_id = _text(stage.get("stage_id"))
        label = _stage_label(stage)
        status = _text(stage.get("status"))
        if _task_kind(stage) == "relaxed_scan":
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
        block, reason = _collect_stage_block(stage)
        if block is None:
            excluded.append(ExcludedStage(stage_id, label, reason))
            if template_name == "conformer_screening":
                incomplete_population_stages.append(label or stage_id or "unknown")
            continue
        if (
            template_name == "conformer_screening"
            and block.kind != "min"
            and (_task_kind(stage) == "opt" or block.kind == "ts")
        ):
            incomplete_population_stages.append(label or stage_id or "unknown")
        entry = WorkflowSiEntry(stage_id=stage_id, block=block)
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
    # Stage IDs in corrupt/legacy payloads are not guaranteed unique. The SiBlock
    # identity is stable through dataclass replacement and keeps this merge 1:1.
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

    # RMSD re-dedup and interaction-energy assembly are additive report-time
    # features isolated behind their own guards: a failure in either omits only
    # that feature and still renders the base SI (methods, table, structures).
    rmsd_groups: tuple[RmsdGroup, ...] = ()
    ranked = pre_dedup_ranked
    if rmsd_cfg is not None:
        try:
            ranked, rmsd_groups = _dedup_minima(pre_dedup_ranked, rmsd_cfg)
        except Exception:  # noqa: BLE001
            if raise_feature_errors:
                raise
            logger.warning("Workflow SI RMSD dedup failed", exc_info=True)
            rmsd_groups = ()
            ranked = pre_dedup_ranked
    unpaired = pre_dedup_unpaired

    interaction_energies: tuple[InteractionEnergyResult, ...] = ()
    if interaction_cfg is not None:
        try:
            interaction_energies = _interaction_energy_results(
                interaction_raw_stages,
                stationary,
                single_points,
                interaction_cfg,
                parameters,
                rmsd_cfg,
            )
        except Exception:  # noqa: BLE001
            if raise_feature_errors:
                raise
            logger.warning("Workflow SI interaction-energy assembly failed", exc_info=True)
            interaction_energies = ()

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
        interaction_energy_enabled=interaction_cfg is not None,
        rmsd_dedup_enabled=rmsd_cfg is not None,
        rmsd_groups=rmsd_groups,
    )


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


# ---------------------------------------------------------------------------
# Methods paragraph
# ---------------------------------------------------------------------------


def _level_key(block: SiBlock) -> tuple[str, str, str, str]:
    result = block.result
    return (result.method, result.basis_set, result.solvation, result.orca_version)


def _level_phrase(method: str, basis: str, solvation: str, version: str) -> str:
    level = "/".join(part for part in (method, basis) if part)
    phrase = f"at the {level} level" if level else "at an unrecognized level of theory"
    if solvation:
        phrase += f" with the {solvation} implicit solvation model"
    if version:
        phrase += f" using ORCA {version}"
    return phrase


def _optimization_sentences(entries: tuple[WorkflowSiEntry, ...]) -> list[str]:
    """One sentence per distinct level, claiming only what actually ran.

    A level mentions harmonic frequency calculations only when at least one of
    its structures carries frequency data: the default conformer-screening
    route is Opt-only, and an SI must not assert frequency analyses that never
    happened (the per-structure ``⚠ uncharacterized`` lint covers stragglers
    when a level mixes Freq and non-Freq jobs).
    """
    levels: list[tuple[tuple[str, str, str, str], bool]] = []
    for entry in entries:
        key = _level_key(entry.block)
        has_freq = entry.block.imaginary_count is not None
        for position, (seen, seen_freq) in enumerate(levels):
            if seen == key:
                levels[position] = (seen, seen_freq or has_freq)
                break
        else:
            levels.append((key, has_freq))
    return [
        (
            "Geometry optimizations and harmonic frequency calculations were performed "
            if has_freq
            else "Geometry optimizations were performed "
        )
        + f"{_level_phrase(*key)}."
        for key, has_freq in levels
    ]


def _funnel_sentence(data: WorkflowSiData) -> str:
    if data.crest_conformer_total is None and data.xtb_candidate_total is None:
        return ""
    parts = []
    if data.crest_conformer_total is not None:
        parts.append(
            f"Conformer ensembles were generated with CREST ({data.crest_conformer_total} conformers)"
        )
    if data.xtb_candidate_total is not None:
        verb = "screened" if parts else "Candidates were screened"
        parts.append(f"{verb} at the xTB level ({data.xtb_candidate_total} candidates)")
    sentence = ", ".join(parts)
    total = len(data.entries)
    if total:
        sentence += f"; {total} structure{'s were' if total != 1 else ' was'} refined with ORCA"
    return sentence + "."


def _characterization_sentence(data: WorkflowSiData) -> str:
    temps = sorted(
        {
            f"{entry.block.result.thermo_temperature_k:.2f}"
            for entry in data.entries
            if entry.block.result.thermo_temperature_k is not None
        }
    )
    if not temps:
        return ""
    return (
        "Stationary points were characterized by harmonic frequency analysis as minima "
        "(Nimag = 0) or transition states (Nimag = 1); thermochemical corrections refer to "
        f"{' / '.join(temps)} K."
    )


def _composite_sentence(data: WorkflowSiData) -> str:
    sp_levels: list[tuple[str, str, str, str]] = []
    for entry in data.entries:
        if entry.sp_block is None:
            continue
        key = _level_key(entry.sp_block)
        if key not in sp_levels:
            sp_levels.append(key)
    if not sp_levels:
        return ""
    phrase = "; ".join(_level_phrase(*level) for level in sp_levels)
    sentence = (
        "Electronic energies were refined by single-point calculations on the optimized "
        f"geometries {phrase}"
    )
    # Claim composite Gibbs energies only when at least one exists: an Opt-only
    # workflow with SP refinements has no G − E(el) correction to combine.
    if any(finite(entry.composite_gibbs) for entry in data.entries):
        sentence += (
            "; composite Gibbs energies combine E(SP) with the G − E(el) "
            "correction from the optimization level"
        )
    return sentence + "."


def _interaction_level_sentences(data: WorkflowSiData) -> list[str]:
    levels: list[tuple[str, str, str, str]] = []
    for result in data.interaction_energies:
        level = (result.method, result.basis_set, result.solvation, result.orca_version)
        if result.input_line and level not in levels:
            levels.append(level)
    return [
        "Interaction-energy single points were performed " + _level_phrase(*level) + "."
        for level in levels
    ]


def _documented_blocks(data: WorkflowSiData) -> list[SiBlock]:
    """Every block whose level the SI must document, including matched SPs."""
    blocks: list[SiBlock] = []
    for entry in data.entries:
        blocks.append(entry.block)
        if entry.sp_block is not None:
            blocks.append(entry.sp_block)
    blocks.extend(entry.block for entry in data.extra_blocks)
    return blocks


def _methods_lines(data: WorkflowSiData) -> list[str]:
    lines: list[str] = []
    funnel = _funnel_sentence(data)
    if funnel:
        lines.append(funnel)
    lines.extend(_optimization_sentences(data.entries))
    characterization = _characterization_sentence(data)
    if characterization:
        lines.append(characterization)
    composite = _composite_sentence(data)
    if composite:
        lines.append(composite)
    lines.extend(_interaction_level_sentences(data))
    routes: list[str] = []
    for block in _documented_blocks(data):
        route = block.result.input_line
        if route and route not in routes:
            routes.append(route)
    for result in data.interaction_energies:
        route = result.input_line
        if route and route not in routes:
            routes.append(route)
    if routes:
        lines.append("")
        lines.append("Route lines as executed:")
        lines.extend(f"  ! {route}" for route in routes)
    return lines


# ---------------------------------------------------------------------------
# Relative-energy table
# ---------------------------------------------------------------------------


def _table_lines(data: WorkflowSiData) -> list[str]:
    candidates = [entry for entry in data.entries if finite(entry.block.result.energy_hartree)]
    if not candidates:
        return ["(no completed stationary structures yet)"]

    convention = _energy_convention(data.entries)

    def gibbs_of(entry: WorkflowSiEntry) -> float | None:
        return (
            entry.composite_gibbs
            if convention.use_composite_gibbs
            else entry.block.result.gibbs_energy
        )

    # Under the refined convention every electronic number in a row is at the
    # SP level: the energy column, the ΔE baseline, and the ranking follow
    # E(SP), not the optimization-level energy — the two orderings can differ,
    # and mixing them would publish wrong relative electronic energies.
    def energy_of(entry: WorkflowSiEntry) -> float:
        if convention.use_single_point_energy:
            assert entry.sp_energy is not None  # guaranteed by the gate
            return entry.sp_energy
        energy = entry.block.result.energy_hartree
        assert energy is not None  # filtered above
        return energy

    rows = sorted(((entry, energy_of(entry)) for entry in candidates), key=lambda pair: pair[1])
    entries = [entry for entry, _ in rows]

    best_e = rows[0][1]
    gibbs_values = [g for g in (gibbs_of(entry) for entry in entries) if finite(g)]
    best_g = min(gibbs_values) if gibbs_values else None

    name_width = max(9, *(len(entry.block.name) for entry in entries))
    e_label = "E(SP)/Eh" if convention.use_single_point_energy else "E(el)/Eh"
    header = (
        f"{'#':>2}  {'structure':<{name_width}}  {e_label:>14}  "
        f"{'G/Eh':>14}  {'ΔE/kcal·mol⁻¹':>14}  {'ΔG/kcal·mol⁻¹':>14}  {'Nimag':>5}"
    )
    lines = [header, "-" * len(header)]
    for rank, (entry, energy) in enumerate(rows, start=1):
        gibbs = gibbs_of(entry)
        gibbs_cell = f"{gibbs:.6f}" if finite(gibbs) else "–"
        rel_e = (energy - best_e) * KCAL_PER_HARTREE
        rel_e_cell = f"{rel_e:+.2f}" if math.isfinite(rel_e) else "–"
        rel_g = (
            (gibbs - best_g) * KCAL_PER_HARTREE if finite(gibbs) and best_g is not None else None
        )
        rel_g_cell = f"{rel_g:+.2f}" if finite(rel_g) else "–"
        nimag = str(entry.block.imaginary_count) if entry.block.imaginary_count is not None else "–"
        lines.append(
            f"{rank:>2}  {entry.block.name:<{name_width}}  {energy:>14.6f}  "
            f"{gibbs_cell:>14}  {rel_e_cell:>14}  {rel_g_cell:>14}  {nimag:>5}"
        )
    notes: list[str] = []
    if convention.use_composite_gibbs:
        notes.append(
            "E, ΔE, and the ranking are at the single-point level; "
            "G is the composite G = E(SP) + [G − E(el)](opt level); "
            "single points paired by unique identical geometry and electronic state."
        )
    elif convention.use_single_point_energy:
        notes.append(
            "E, ΔE, and the ranking are at the single-point level; "
            "G and ΔG are at the optimization level."
        )
    if convention.note:
        notes.append(f"⚠ {convention.note}.")
    if data.rmsd_dedup_enabled:
        merged = sum(group.degeneracy - 1 for group in data.rmsd_groups)
        if merged > 0:
            notes.append(
                f"Minima are RMSD representatives; {merged} degenerate "
                "minima were merged (per-structure degeneracy in si_data.csv)."
            )
    if notes:
        lines.append("")
        lines.extend(notes)
    return lines


# ---------------------------------------------------------------------------
# Rendering / writing
# ---------------------------------------------------------------------------


def _aligned_populations(data: WorkflowSiData) -> tuple[PopulationRow | None, ...]:
    if len(data.populations) == len(data.entries):
        return data.populations
    return tuple(None for _ in data.entries)


def _population_lines(data: WorkflowSiData) -> list[str]:
    """Body of ``## Boltzmann populations`` — [] omits the whole section."""
    if not any(entry.block.kind == "min" for entry in data.entries):
        return [data.population_note] if data.population_note else []
    populated = [
        (entry, row)
        for entry, row in zip(data.entries, _aligned_populations(data), strict=True)
        if row is not None and row.population is not None
    ]
    if not populated:
        return [data.population_note or _POP_NO_GIBBS_NOTE]

    temperature = data.boltzmann_temperature_k
    assert temperature is not None  # a populated set implies a resolved temperature
    lines = [
        f"Boltzmann populations at {temperature:.2f} K "
        f"({data.boltzmann_temperature_source}), normalized within each "
        "formula|charge|multiplicity group (an equilibrium over that group's minima).",
        "",
    ]
    clusters: dict[str, list[tuple[WorkflowSiEntry, PopulationRow]]] = {}
    for entry, row in populated:
        clusters.setdefault(row.cluster_key, []).append((entry, row))
    multi = len(clusters) > 1
    for key, members in clusters.items():
        members.sort(key=lambda pair: pair[1].rel_g_kcalmol or 0.0)
        if multi:
            lines.append(f"group {key}")
        name_width = max(9, *(len(entry.block.name) for entry, _ in members))
        header = (
            f"{'#':>2}  {'structure':<{name_width}}  {'ΔG/kcal·mol⁻¹':>14}  {'population/%':>12}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for rank, (entry, row) in enumerate(members, start=1):
            rel_g = row.rel_g_kcalmol if row.rel_g_kcalmol is not None else 0.0
            population = (row.population or 0.0) * 100.0
            lines.append(
                f"{rank:>2}  {entry.block.name:<{name_width}}  {rel_g:>+14.2f}  {population:>12.2f}"
            )
        lines.append("")
    if data.population_note:
        lines.append(data.population_note)
    return lines


def _interaction_energy_lines(data: WorkflowSiData) -> list[str]:
    """Body of ``## Interaction energies`` — [] omits the whole section."""
    if not data.interaction_energies:
        return []
    lines = [
        "Interaction energies ΔE_int = E(complex) − Σ E(fragment), from same-level "
        "single points on the complex-optimized geometry.",
        "No separate Boys–Bernardi ghost-atom counterpoise calculation was performed; "
        "method-inherent corrections (for example, r2SCAN-3c gCP) remain part of the "
        "stated method.",
        "",
    ]
    for result in data.interaction_energies:
        parent = result.parent_stage_id or result.complex_stage_id or "unidentified parent"
        header = f"{result.complex_label} ({parent})"
        if result.resolved:
            assert result.de_int_hartree is not None and result.de_int_kcalmol is not None
            lines.append(
                f"{header}: ΔE_int = {result.de_int_hartree:.6f} Eh "
                f"({result.de_int_kcalmol:+.2f} kcal·mol⁻¹)"
            )
        else:
            lines.append(f"{header}: ΔE_int omitted — {result.note or 'incomplete data'}")
        if result.input_line:
            lines.append(f"  executed route: ! {result.input_line} (ORCA {result.orca_version})")
        if result.complex_stage_id:
            lines.append(f"  complex single-point stage: {result.complex_stage_id}")
        for fragment in result.fragments:
            energy_cell = (
                f"{fragment.energy_hartree:16.6f} Eh" if finite(fragment.energy_hartree) else "–"
            )
            lines.append(
                f"  {fragment.label} [atoms {','.join(str(i) for i in fragment.atom_indices)}] "
                f"(charge {fragment.charge}, multiplicity {fragment.multiplicity}): {energy_cell}"
            )
        if result.resolved and result.note:
            lines.append(f"  ⚠ {result.note}")
        lines.append("")
    return lines


def render_workflow_si_md(data: WorkflowSiData) -> str:
    lines = [f"# Supporting Information — {data.workflow_id}"]
    meta = [f"template {data.template_name}" if data.template_name else ""]
    if data.reaction_key:
        meta.append(f"reaction {data.reaction_key}")
    meta_text = " · ".join(part for part in meta if part)
    if meta_text:
        lines.append(meta_text)
    lines.append("")

    lines.append("## Computational details")
    lines.append("")
    lines.extend(_methods_lines(data))
    lines.append("")

    lines.append("## Relative energies")
    lines.append("")
    lines.extend(_table_lines(data))
    lines.append("")

    population_lines = _population_lines(data)
    if population_lines:
        lines.append("## Boltzmann populations")
        lines.append("")
        lines.extend(population_lines)
        lines.append("")

    interaction_lines = _interaction_energy_lines(data)
    if interaction_lines:
        lines.append("## Interaction energies")
        lines.append("")
        lines.extend(interaction_lines)
        lines.append("")

    lines.append("## Structures")
    lines.append("")
    for entry in data.entries:
        lines.append(render_si_block_md(entry.block))
        if finite(entry.sp_energy):
            note = f"E(SP) = {entry.sp_energy:16.6f} Eh  ({entry.sp_label})"
            if entry.sp_block is not None and entry.sp_block.result.input_line:
                note += f"\n  ! {entry.sp_block.result.input_line}"
            if finite(entry.composite_gibbs):
                note += f"\nG(composite) = {entry.composite_gibbs:16.6f} Eh"
            lines.append(note)
            lines.append("")
    for entry in data.extra_blocks:
        lines.append(render_si_block_md(entry.block))

    if data.excluded:
        lines.append("## Excluded jobs")
        lines.append("")
        lines.extend(
            f"- {stage.label} ({stage.stage_id}): {stage.reason}" for stage in data.excluded
        )
        lines.append("")

    lines.append("Generated by orca_auto · assembled from completed stages only")
    lines.append("")
    return "\n".join(lines)


_CSV_COLUMNS = [
    "name",
    "stage_id",
    "kind",
    "formula",
    "charge",
    "multiplicity",
    "method",
    "basis_set",
    "solvation",
    "orca_version",
    "route",
    "E_Eh",
    "ZPE_Eh",
    "H_Eh",
    "G_Eh",
    "G_minus_Eel_Eh",
    "sp_method",
    "sp_basis_set",
    "sp_solvation",
    "sp_orca_version",
    "sp_route",
    "E_SP_Eh",
    "G_composite_Eh",
    "Nimag",
    "lowest_freq_cm1",
    "temperature_K",
    "warnings",
    # Appended after the frozen 27-column schema; header-keyed and
    # unknown-field-ignoring consumers, and positional readers of the first 27
    # fields, are unaffected.
    "cluster_key",
    "rel_E_kcalmol",
    "rel_G_kcalmol",
    "boltzmann_T_K",
    "boltzmann_population",
]

# Appended to si_data.csv ONLY when rmsd_dedup is enabled, so the file stays
# byte-identical to the feature-off baseline when it is not.
_RMSD_DEDUP_CSV_COLUMNS = ["rmsd_group", "degeneracy", "merged_stage_ids"]

_INTERACTION_CSV_COLUMNS = [
    "parent_stage_id",
    "complex_stage_id",
    "complex_label",
    "complex_charge",
    "complex_multiplicity",
    "complex_formula",
    "E_complex_Eh",
    "method",
    "basis_set",
    "solvation",
    "orca_version",
    "route_line",
    "ghost_counterpoise_applied",
    "fragment_label",
    "fragment_stage_id",
    "fragment_atom_indices",
    "fragment_formula",
    "fragment_charge",
    "fragment_multiplicity",
    "E_fragment_Eh",
    "dE_int_Eh",
    "dE_int_kcalmol",
    "note",
]


def _finite_or_blank(value: float | None) -> float | str:
    return value if finite(value) else ""


def _spreadsheet_safe_text(value: Any) -> str:
    """Neutralize formula-leading text when a CSV is opened in a spreadsheet."""
    text = _text(value)
    if text and text[0] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def _rmsd_dedup_cells(data: WorkflowSiData, entry: WorkflowSiEntry) -> list[Any]:
    lookup = data.rmsd_group_for(entry.stage_id)
    if lookup is None:
        return ["", "", ""]
    index, group = lookup
    return [index, group.degeneracy, ";".join(group.merged_stage_ids)]


def _si_csv_row(
    entry: WorkflowSiEntry,
    population: PopulationRow | None,
    temperature: float | None,
) -> list[Any]:
    result = entry.block.result
    sp = entry.sp_block.result if entry.sp_block is not None else None
    return [
        entry.block.name,
        entry.stage_id,
        entry.block.kind,
        result.formula,
        result.charge,
        result.multiplicity,
        result.method,
        result.basis_set,
        result.solvation,
        result.orca_version,
        result.input_line,
        _finite_or_blank(result.energy_hartree),
        _finite_or_blank(result.zpe_correction),
        _finite_or_blank(result.enthalpy),
        _finite_or_blank(result.gibbs_energy),
        _finite_or_blank(result.gibbs_correction),
        sp.method if sp is not None else "",
        sp.basis_set if sp is not None else "",
        sp.solvation if sp is not None else "",
        sp.orca_version if sp is not None else "",
        sp.input_line if sp is not None else "",
        _finite_or_blank(entry.sp_energy),
        _finite_or_blank(entry.composite_gibbs),
        entry.block.imaginary_count,
        _finite_or_blank(result.lowest_freq_cm1),
        _finite_or_blank(result.thermo_temperature_k),
        "; ".join(entry.block.warnings),
        population.cluster_key if population is not None else "",
        _finite_or_blank(population.rel_e_kcalmol) if population is not None else "",
        _finite_or_blank(population.rel_g_kcalmol) if population is not None else "",
        (
            _finite_or_blank(temperature)
            if population is not None and population.population is not None
            else ""
        ),
        _finite_or_blank(population.population) if population is not None else "",
    ]


def render_workflow_si_csv(data: WorkflowSiData) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    columns = list(_CSV_COLUMNS)
    if data.rmsd_dedup_enabled:
        columns = columns + _RMSD_DEDUP_CSV_COLUMNS
    writer.writerow(columns)
    # Populations align 1:1 with entries; extra blocks (unpaired SPs) are never
    # populated. An empty populations tuple means no minima → all-blank cells.
    populations = _aligned_populations(data)

    def emit(entry: WorkflowSiEntry, population: PopulationRow | None) -> None:
        row = _si_csv_row(entry, population, data.boltzmann_temperature_k)
        if data.rmsd_dedup_enabled:
            row = row + _rmsd_dedup_cells(data, entry)
        writer.writerow(row)

    for entry, population in zip(data.entries, populations, strict=True):
        emit(entry, population)
    for entry in data.extra_blocks:
        emit(entry, None)
    return buffer.getvalue()


def render_interaction_energy_csv(data: WorkflowSiData) -> str | None:
    """Machine-readable ΔE_int table; ``None`` when the feature produced nothing."""
    if not data.interaction_energies:
        return None
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_INTERACTION_CSV_COLUMNS)
    for result in data.interaction_energies:
        for fragment in result.fragments:
            writer.writerow(
                [
                    _spreadsheet_safe_text(result.parent_stage_id),
                    _spreadsheet_safe_text(result.complex_stage_id),
                    _spreadsheet_safe_text(result.complex_label),
                    result.complex_charge,
                    result.complex_multiplicity,
                    _spreadsheet_safe_text(result.complex_formula),
                    _finite_or_blank(result.complex_energy_hartree),
                    _spreadsheet_safe_text(result.method),
                    _spreadsheet_safe_text(result.basis_set),
                    _spreadsheet_safe_text(result.solvation),
                    _spreadsheet_safe_text(result.orca_version),
                    _spreadsheet_safe_text(result.input_line),
                    # Counterpoise is not implemented; the provenance column is a
                    # constant so downstream readers need not special-case it.
                    "false",
                    _spreadsheet_safe_text(fragment.label),
                    _spreadsheet_safe_text(fragment.stage_id),
                    _spreadsheet_safe_text(";".join(str(index) for index in fragment.atom_indices)),
                    _spreadsheet_safe_text(fragment.formula),
                    fragment.charge,
                    fragment.multiplicity,
                    _finite_or_blank(fragment.energy_hartree),
                    _finite_or_blank(result.de_int_hartree),
                    _finite_or_blank(result.de_int_kcalmol),
                    _spreadsheet_safe_text(result.note),
                ]
            )
    return buffer.getvalue()


def _request_parameters(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    request = metadata.get("request")
    if not isinstance(request, dict):
        return {}
    parameters = request.get("parameters")
    return parameters if isinstance(parameters, dict) else {}


def _boltzmann_temperature_override(
    payload: Mapping[str, Any],
) -> tuple[float | None, str]:
    """Read the admission-validated override from durable workflow state only."""
    parameters = _request_parameters(payload)
    if "boltzmann_temperature_k" not in parameters:
        return None, ""
    try:
        return optional_positive_float(parameters, "boltzmann_temperature_k"), ""
    except ValueError:
        logger.warning("Invalid durable boltzmann_temperature_k", exc_info=True)
        return None, "(populations omitted: durable boltzmann_temperature_k is invalid)"


def _remove_si_artifacts(*paths: Path, raise_on_error: bool = False) -> None:
    first_error: OSError | None = None
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            first_error = first_error or exc
            logger.warning("Failed to remove inconsistent SI artifact %s", path, exc_info=True)
    if raise_on_error and first_error is not None:
        raise first_error


def _interaction_content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _interaction_owner_identity(workflow_id: str) -> str:
    return hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _interaction_owner_text(
    workflow_id: str,
    digest: str,
    *,
    pending_digest: str = "-",
) -> str:
    return (
        "orca_auto interaction_energy.csv owner v2\n"
        f"workflow-sha256:{_interaction_owner_identity(workflow_id)}\n"
        f"sha256:{digest}\npending-sha256:{pending_digest}\n"
    )


def _read_interaction_owner(owner_path: Path) -> tuple[str, str, str] | None:
    if not owner_path.is_file() or owner_path.is_symlink():
        return None
    try:
        lines = owner_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) != 4 or lines[0] != "orca_auto interaction_energy.csv owner v2":
        return None
    if not lines[2].startswith("sha256:") or not lines[3].startswith("pending-sha256:"):
        return None
    digest = lines[2][7:]
    pending_digest = lines[3][15:]
    if any(
        value != "-" and (len(value) != 64 or re.fullmatch(r"[0-9a-f]{64}", value) is None)
        for value in (digest, pending_digest)
    ):
        return None
    if not lines[1].startswith("workflow-sha256:"):
        return None
    identity = lines[1][16:]
    if len(identity) != 64 or re.fullmatch(r"[0-9a-f]{64}", identity) is None:
        return None
    return identity, digest, pending_digest


def _owned_interaction_artifact(
    interaction_path: Path,
    owner_path: Path,
    *,
    workflow_id: str,
) -> bool:
    if not interaction_path.is_file() or interaction_path.is_symlink():
        return False
    owner = _read_interaction_owner(owner_path)
    if owner is None or owner[0] != _interaction_owner_identity(workflow_id):
        return False
    try:
        digest = _file_sha256(interaction_path)
    except OSError:
        return False
    return digest in {owner[1], owner[2]} - {"-"}


def _write_owned_interaction_artifact(
    interaction_path: Path,
    owner_path: Path,
    *,
    workflow_id: str,
    text: str,
) -> None:
    desired_digest = _interaction_content_digest(text)
    current_digest = "-"
    if interaction_path.exists() or interaction_path.is_symlink():
        if not _owned_interaction_artifact(interaction_path, owner_path, workflow_id=workflow_id):
            raise FileExistsError(
                f"refusing to overwrite unowned or modified interaction-energy artifact: "
                f"{interaction_path}"
            )
        current_digest = _file_sha256(interaction_path)
    elif owner_path.exists() or owner_path.is_symlink():
        owner = _read_interaction_owner(owner_path)
        if owner is None or owner[0] != _interaction_owner_identity(workflow_id):
            raise FileExistsError(
                f"interaction-energy artifact owner marker is not owned by {workflow_id}"
            )
    atomic_write_text(
        owner_path,
        _interaction_owner_text(
            workflow_id,
            current_digest,
            pending_digest=desired_digest,
        ),
    )
    atomic_write_text(interaction_path, text)
    atomic_write_text(
        owner_path,
        _interaction_owner_text(workflow_id, desired_digest),
    )


def _preflight_interaction_artifact_write(
    interaction_path: Path,
    owner_path: Path,
    *,
    workflow_id: str,
) -> None:
    """Reject an unowned/conflicting target before base SI files are touched."""
    if interaction_path.exists() or interaction_path.is_symlink():
        if not _owned_interaction_artifact(interaction_path, owner_path, workflow_id=workflow_id):
            owner = _read_interaction_owner(owner_path)
            if owner is not None and owner[0] == _interaction_owner_identity(workflow_id):
                # The data was edited/replaced after publication. Preserve it,
                # but release this workflow's stale authority before blocking.
                owner_path.unlink(missing_ok=True)
            raise FileExistsError(
                f"refusing to overwrite unowned or modified interaction-energy artifact: "
                f"{interaction_path}"
            )
        return
    if owner_path.exists() or owner_path.is_symlink():
        owner = _read_interaction_owner(owner_path)
        if owner is None or owner[0] != _interaction_owner_identity(workflow_id):
            raise FileExistsError(
                f"interaction-energy artifact owner marker is not owned by {workflow_id}"
            )


def _remove_owned_interaction_artifact(
    interaction_path: Path,
    owner_path: Path,
    *,
    workflow_id: str,
) -> None:
    owner = _read_interaction_owner(owner_path)
    if owner is None or owner[0] != _interaction_owner_identity(workflow_id):
        return
    if not interaction_path.exists():
        owner_path.unlink(missing_ok=True)
        return
    if interaction_path.is_symlink() or not _owned_interaction_artifact(
        interaction_path, owner_path, workflow_id=workflow_id
    ):
        # Preserve a user-replaced or edited file, and release the stale marker
        # so it can never authorize an overwrite.
        owner_path.unlink(missing_ok=True)
        return
    interaction_path.unlink()
    owner_path.unlink(missing_ok=True)


def write_workflow_si(
    workspace_dir: Path,
    payload: Mapping[str, Any],
    *,
    raise_on_error: bool = False,
) -> Path | None:
    """Write ``workflow_si.md`` + ``si_data.csv`` (+ ``interaction_energy.csv``).

    A workflow without ORCA stages has no SI: stale files from an earlier
    template are removed so nothing obsolete can be pasted into a paper. The
    interaction-energy CSV is written only when ΔE_int data exists and is removed
    otherwise so a disabled feature never leaves a stale table behind.
    Errors are logged and suppressed by default; ``raise_on_error=True`` exposes
    them to the durable publication retry state machine.
    """
    md_path = workspace_dir / WORKFLOW_SI_MD_FILE
    csv_path = workspace_dir / WORKFLOW_SI_CSV_FILE
    interaction_path = workspace_dir / INTERACTION_ENERGY_CSV_FILE
    interaction_owner_path = workspace_dir / INTERACTION_ENERGY_CSV_OWNER_FILE
    workflow_id = _text(payload.get("workflow_id"))
    try:
        # Durable corruption is not equivalent to an explicit feature disable.
        # Validate before touching the last known-good publication.
        parameters = _request_parameters(payload)
        normalized_interaction = normalize_interaction_energy_block(
            parameters.get("interaction_energy")
        )
        # Evaluate the strict charge/multiplicity reads only when the feature is
        # configured: corrupt request parameters must fail the interaction
        # feature closed, not block the whole SI of an unrelated workflow.
        if normalized_interaction is not None:
            validate_interaction_energy_state_balance(
                normalized_interaction,
                complex_charge=require_int(parameters.get("charge", 0), field="charge"),
                complex_multiplicity=require_int(
                    parameters.get("multiplicity", 1), field="multiplicity", minimum=1
                ),
            )
        normalized_rmsd = normalize_rmsd_dedup_block(parameters.get("rmsd_dedup"))
        validate_conformer_postprocessing_template(
            payload.get("template_name"),
            interaction_energy=normalized_interaction,
            rmsd_dedup=normalized_rmsd,
        )
        override, population_blocker = _boltzmann_temperature_override(payload)
        data = collect_workflow_si_data(
            payload,
            boltzmann_temperature_k=override,
            population_blocker=population_blocker,
            raise_feature_errors=True,
        )
        if not data.has_orca_stages():
            _remove_si_artifacts(md_path, csv_path, raise_on_error=raise_on_error)
            _remove_owned_interaction_artifact(
                interaction_path,
                interaction_owner_path,
                workflow_id=workflow_id,
            )
            return None
        # Render every document before writing any, so a rendering failure leaves
        # the previous consistent set on disk instead of a fresh md next to a
        # stale csv.
        md_text = render_workflow_si_md(data)
        csv_text = render_workflow_si_csv(data)
        interaction_text = render_interaction_energy_csv(data)
        if interaction_text is not None:
            # A deterministic ownership conflict must not replace/delete the
            # already published base SI before the durable state is blocked.
            _preflight_interaction_artifact_write(
                interaction_path,
                interaction_owner_path,
                workflow_id=workflow_id,
            )
        try:
            atomic_write_text(md_path, md_text)
            atomic_write_text(csv_path, csv_text)
            if interaction_text is not None:
                _write_owned_interaction_artifact(
                    interaction_path,
                    interaction_owner_path,
                    workflow_id=workflow_id,
                    text=interaction_text,
                )
            else:
                _remove_owned_interaction_artifact(
                    interaction_path,
                    interaction_owner_path,
                    workflow_id=workflow_id,
                )
        except Exception:
            # A caught mid-write failure must never leave an inconsistent mix of
            # fresh and stale SI artifacts. All of them are reproducible.
            _remove_si_artifacts(md_path, csv_path)
            _remove_owned_interaction_artifact(
                interaction_path,
                interaction_owner_path,
                workflow_id=workflow_id,
            )
            raise
        return md_path
    except Exception:  # noqa: BLE001
        logger.warning("Workflow SI generation failed for %s", workspace_dir, exc_info=True)
        if raise_on_error:
            raise
        return None


__all__ = [
    "ExcludedStage",
    "PopulationRow",
    "WorkflowSiData",
    "WorkflowSiEntry",
    "collect_workflow_si_data",
    "render_interaction_energy_csv",
    "render_workflow_si_csv",
    "render_workflow_si_md",
    "write_workflow_si",
]
