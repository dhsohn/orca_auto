"""Copy-paste-ready Supporting Information for a whole workflow run.

``workflow_si.md`` assembles what a paper's SI needs from every completed
ORCA stage: a computational-details paragraph generated from the routes and
program versions that actually ran (not from memory), the CREST → xTB → ORCA
funnel provenance, a relative-energy table, and each structure's SI block.
``si_data.csv`` carries the same numbers in machine-readable form for
data-availability requirements.

When an opt+freq structure has a matching single-point stage (identical
geometry within 1e-4 Å), the table adds a composite
G = E(SP) + [G − E(el)](opt level); the geometry match IS the pairing rule,
so an SP run on the wrong geometry can never silently refine the wrong
structure. Rewritten on every workflow advance and assembled from completed
stages only, so a partial workflow yields a partial (still valid) SI.
Generation must never break the advance: errors are logged and swallowed.
"""

from __future__ import annotations

import csv
import io
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import WORKFLOW_SI_CSV_FILE, WORKFLOW_SI_MD_FILE
from orca_auto.core.utils.persistence import atomic_write_text
from orca_auto.orca.report.render import KCAL_PER_HARTREE, R_KCAL_PER_MOL_K
from orca_auto.orca.report.si import (
    SiBlock,
    SiBlockError,
    collect_si_block,
    render_si_block_md,
)
from orca_auto.orca.state import load_state

from ..manifest import load_flow_manifest
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

# Two geometries printed by ORCA to 6 decimals are "the same structure" well
# below this; an SP run on anything else (reordered atoms, re-optimized
# geometry) must not pair.
_GEOMETRY_TOL_ANGSTROM = 1e-4

# Boltzmann populations are physical only among interconverting minima of one
# species. This groups minima by formula/charge/multiplicity as a stoichiometric
# proxy for "one species" (it does not read connectivity, so constitutional
# isomers of the same formula pool together as an equilibrium over those minima)
# and requires a Gibbs free energy. Transition states and cross-group mixing are
# excluded, and populations are omitted (never fabricated) when the temperature is
# missing or ambiguous. ORCA prints THERMOCHEMISTRY AT to 2 decimals, so this
# tolerance only guards rounding and mixed-temperature runs.
_TEMP_TOLERANCE_K = 0.5
_POP_NO_GIBBS_NOTE = (
    "(populations require Gibbs free energies over minima; none available — "
    "add a frequency calculation to enable them)"
)


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

    def has_orca_stages(self) -> bool:
        return bool(self.entries or self.extra_blocks or self.excluded)


def _geometry_matches(a: SiBlock, b: SiBlock) -> bool:
    coords_a, coords_b = a.result.coordinates, b.result.coordinates
    if len(coords_a) != len(coords_b) or not coords_a:
        return False
    for (el_a, *xyz_a), (el_b, *xyz_b) in zip(coords_a, coords_b, strict=True):
        if el_a != el_b:
            return False
        if any(abs(va - vb) > _GEOMETRY_TOL_ANGSTROM for va, vb in zip(xyz_a, xyz_b, strict=True)):
            return False
    return True


def _stage_label(stage: Mapping[str, Any]) -> str:
    return _text(_stage_metadata(stage).get("selected_input_label")) or _text(stage.get("stage_id"))


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
    return replace(block, name=_stage_label(stage)), ""


def _pair_single_points(
    stationary: list[WorkflowSiEntry],
    single_points: list[WorkflowSiEntry],
) -> tuple[list[WorkflowSiEntry], list[WorkflowSiEntry]]:
    """Fold each SP onto the stationary structure with the identical geometry."""
    paired: list[WorkflowSiEntry] = []
    unused = list(single_points)
    for entry in stationary:
        correction = entry.block.result.gibbs_correction
        match = next(
            (
                sp
                for sp in unused
                if sp.block.result.energy_hartree is not None
                and _geometry_matches(entry.block, sp.block)
            ),
            None,
        )
        if match is None:
            paired.append(entry)
            continue
        unused.remove(match)
        sp_energy = match.block.result.energy_hartree
        assert sp_energy is not None  # guaranteed by the match predicate
        composite = sp_energy + correction if correction is not None else None
        paired.append(
            replace(
                entry,
                sp_block=match.block,
                sp_energy=sp_energy,
                sp_label=match.block.name,
                composite_gibbs=composite,
            )
        )
    return paired, unused


def collect_workflow_si_data(
    payload: Mapping[str, Any],
    *,
    boltzmann_temperature_k: float | None = None,
) -> WorkflowSiData:
    crest_total: int | None = None
    xtb_total: int | None = None
    stationary: list[WorkflowSiEntry] = []
    single_points: list[WorkflowSiEntry] = []
    extra: list[WorkflowSiEntry] = []
    excluded: list[ExcludedStage] = []

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
            continue
        block, reason = _collect_stage_block(stage)
        if block is None:
            excluded.append(ExcludedStage(stage_id, label, reason))
            continue
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
    ranked, unpaired = _pair_single_points(stationary, single_points)

    # A population bug must never replace a valid SI with stale files: isolate the
    # computation so the base document (methods, relative-energy table, structures)
    # still renders even if this raises.
    try:
        populations, temperature, temperature_source, population_note = _compute_populations(
            tuple(ranked), boltzmann_temperature_k
        )
    except Exception:  # noqa: BLE001
        logger.warning("Boltzmann population computation failed", exc_info=True)
        populations, temperature, temperature_source, population_note = (), None, "", ""

    return WorkflowSiData(
        workflow_id=_text(payload.get("workflow_id")),
        template_name=_text(payload.get("template_name")),
        status=_text(payload.get("status")),
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
    )


# ---------------------------------------------------------------------------
# Boltzmann populations
# ---------------------------------------------------------------------------


def _cluster_key(entry: WorkflowSiEntry) -> str:
    result = entry.block.result
    return f"{result.formula or '?'}|{result.charge}|{result.multiplicity}"


def _compute_populations(
    entries: tuple[WorkflowSiEntry, ...],
    override: float | None,
) -> tuple[tuple[PopulationRow | None, ...], float | None, str, str]:
    """Per-species Boltzmann populations over minima; fail closed to omission.

    Returns ``(rows, temperature, source, note)``. ``rows`` aligns 1:1 with
    ``entries``; it is an empty tuple when the workflow has no minima at all (the
    section is suppressed), and a length-matched tuple of mostly ``None`` when
    minima exist but populations cannot be computed (the section shows ``note``).
    Only ``kind == "min"`` entries with a Gibbs energy and a parsed thermochemistry
    temperature are populated, and each species is normalized independently.
    """
    min_indices = [i for i, entry in enumerate(entries) if entry.block.kind == "min"]
    if not min_indices:
        return (), None, "", ""

    usable = [
        i
        for i in min_indices
        if entries[i].block.result.gibbs_energy is not None
        and entries[i].block.result.thermo_temperature_k is not None
    ]
    blank: tuple[PopulationRow | None, ...] = tuple(None for _ in entries)
    if not usable:
        return blank, None, "", _POP_NO_GIBBS_NOTE

    temps = [t for i in usable if (t := entries[i].block.result.thermo_temperature_k) is not None]
    t_low, t_high = min(temps), max(temps)
    # Pooled minima must share one frequency temperature: a Gibbs energy embeds
    # −T·S at the job's own T, so mixing temperatures is unphysical no matter which
    # temperature the weighting uses. The manifest key only pins/labels that shared
    # temperature — it cannot reconcile genuinely disagreeing frequency jobs.
    if t_high - t_low > _TEMP_TOLERANCE_K:
        return (
            blank,
            None,
            "",
            "(populations omitted: thermochemistry temperatures disagree — "
            f"{t_low:.2f}–{t_high:.2f} K; populations need one frequency temperature)",
        )
    if override is not None:
        if abs(override - t_low) > _TEMP_TOLERANCE_K or abs(override - t_high) > _TEMP_TOLERANCE_K:
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

    clusters: dict[str, list[int]] = {}
    for i in usable:
        clusters.setdefault(_cluster_key(entries[i]), []).append(i)

    rows: list[PopulationRow | None] = [None] * len(entries)
    rt = R_KCAL_PER_MOL_K * temperature
    for key, members in clusters.items():
        # Composite G (E(SP) + [G−E(el)]) and its E(SP) are used only when every
        # member has one AND the paired single points share one level of theory,
        # so the composite electronic baseline stays consistent within the species.
        composite_ready = all(entries[i].composite_gibbs is not None for i in members)
        sp_levels = {
            _level_key(sp) for sp in (entries[i].sp_block for i in members) if sp is not None
        }
        use_composite = composite_ready and len(sp_levels) <= 1
        gibbs: dict[int, float] = {}
        energy: dict[int, float | None] = {}
        for i in members:
            entry = entries[i]
            value = entry.composite_gibbs if use_composite else entry.block.result.gibbs_energy
            assert value is not None  # usable + all-composite gate guarantee this
            gibbs[i] = value
            energy[i] = entry.sp_energy if use_composite else entry.block.result.energy_hartree
        g_min = min(gibbs.values())
        present_energy = [e for e in energy.values() if e is not None]
        e_min = min(present_energy) if present_energy else None
        weights = {i: math.exp(-((gibbs[i] - g_min) * KCAL_PER_HARTREE) / rt) for i in members}
        partition = sum(weights.values())
        for i in members:
            e_i = energy[i]
            rows[i] = PopulationRow(
                cluster_key=key,
                rel_e_kcalmol=(
                    (e_i - e_min) * KCAL_PER_HARTREE
                    if e_i is not None and e_min is not None
                    else None
                ),
                rel_g_kcalmol=(gibbs[i] - g_min) * KCAL_PER_HARTREE,
                population=weights[i] / partition if partition > 0 else None,
            )

    covered = sum(1 for row in rows if row is not None)
    note = ""
    if covered < len(min_indices):
        note = (
            f"⚠ populations cover {covered} of {len(min_indices)} minima; "
            "structures without Gibbs free energies are omitted"
        )
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
    if any(entry.composite_gibbs is not None for entry in data.entries):
        sentence += (
            "; composite Gibbs energies combine E(SP) with the G − E(el) "
            "correction from the optimization level"
        )
    return sentence + "."


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
    routes: list[str] = []
    for block in _documented_blocks(data):
        route = block.result.input_line
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
    candidates = [entry for entry in data.entries if entry.block.result.energy_hartree is not None]
    if not candidates:
        return ["(no completed stationary structures yet)"]

    # The SP-energy convention is gated on the refinements alone: an Opt-only
    # workflow with SP refinements has E(SP) for every structure but no
    # G − E(el) correction, and its E/ΔE must still be at the SP level — the
    # composite-G convention is a separate, stricter gate on top of it.
    all_refined = all(entry.sp_energy is not None for entry in candidates)
    all_composite = all(entry.composite_gibbs is not None for entry in candidates)

    def gibbs_of(entry: WorkflowSiEntry) -> float | None:
        return entry.composite_gibbs if all_composite else entry.block.result.gibbs_energy

    # Under the refined convention every electronic number in a row is at the
    # SP level: the energy column, the ΔE baseline, and the ranking follow
    # E(SP), not the optimization-level energy — the two orderings can differ,
    # and mixing them would publish wrong relative electronic energies.
    def energy_of(entry: WorkflowSiEntry) -> float:
        if all_refined:
            assert entry.sp_energy is not None  # guaranteed by the gate
            return entry.sp_energy
        energy = entry.block.result.energy_hartree
        assert energy is not None  # filtered above
        return energy

    rows = sorted(((entry, energy_of(entry)) for entry in candidates), key=lambda pair: pair[1])
    entries = [entry for entry, _ in rows]

    best_e = rows[0][1]
    gibbs_values = [g for g in (gibbs_of(entry) for entry in entries) if g is not None]
    best_g = min(gibbs_values) if gibbs_values else None

    name_width = max(9, *(len(entry.block.name) for entry in entries))
    e_label = "E(SP)/Eh" if all_refined else "E(el)/Eh"
    header = (
        f"{'#':>2}  {'structure':<{name_width}}  {e_label:>14}  "
        f"{'G/Eh':>14}  {'ΔE/kcal·mol⁻¹':>14}  {'ΔG/kcal·mol⁻¹':>14}  {'Nimag':>5}"
    )
    lines = [header, "-" * len(header)]
    for rank, (entry, energy) in enumerate(rows, start=1):
        gibbs = gibbs_of(entry)
        gibbs_cell = f"{gibbs:.6f}" if gibbs is not None else "–"
        rel_e = (energy - best_e) * KCAL_PER_HARTREE
        rel_g_cell = (
            f"{(gibbs - best_g) * KCAL_PER_HARTREE:+.2f}"
            if gibbs is not None and best_g is not None
            else "–"
        )
        nimag = str(entry.block.imaginary_count) if entry.block.imaginary_count is not None else "–"
        lines.append(
            f"{rank:>2}  {entry.block.name:<{name_width}}  {energy:>14.6f}  "
            f"{gibbs_cell:>14}  {rel_e:>+14.2f}  {rel_g_cell:>14}  {nimag:>5}"
        )
    notes: list[str] = []
    if all_composite:
        notes.append(
            "E, ΔE, and the ranking are at the single-point level; "
            "G is the composite G = E(SP) + [G − E(el)](opt level); "
            "single points paired by identical geometry."
        )
    else:
        if all_refined:
            notes.append(
                "E, ΔE, and the ranking are at the single-point level; "
                "single points paired by identical geometry."
            )
        elif any(entry.sp_energy is not None for entry in entries):
            notes.append(
                "⚠ single-point refinements cover only part of the set; "
                "E and ΔE use the optimization level for all entries to keep one baseline."
            )
        if any(entry.composite_gibbs is not None for entry in entries):
            notes.append(
                "⚠ composite G available for only part of the set; "
                "ΔG uses the optimization-level G for all entries to keep one baseline."
            )
    if notes:
        lines.append("")
        lines.extend(notes)
    return lines


# ---------------------------------------------------------------------------
# Rendering / writing
# ---------------------------------------------------------------------------


def _population_lines(data: WorkflowSiData) -> list[str]:
    """Body of ``## Boltzmann populations`` — [] omits the whole section."""
    if not any(entry.block.kind == "min" for entry in data.entries):
        return []
    populated = [
        (entry, row)
        for entry, row in zip(data.entries, data.populations, strict=False)
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

    lines.append("## Structures")
    lines.append("")
    for entry in data.entries:
        lines.append(render_si_block_md(entry.block))
        if entry.sp_energy is not None:
            note = f"E(SP) = {entry.sp_energy:16.6f} Eh  ({entry.sp_label})"
            if entry.sp_block is not None and entry.sp_block.result.input_line:
                note += f"\n  ! {entry.sp_block.result.input_line}"
            if entry.composite_gibbs is not None:
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
        result.energy_hartree,
        result.zpe_correction,
        result.enthalpy,
        result.gibbs_energy,
        result.gibbs_correction,
        sp.method if sp is not None else "",
        sp.basis_set if sp is not None else "",
        sp.solvation if sp is not None else "",
        sp.orca_version if sp is not None else "",
        sp.input_line if sp is not None else "",
        entry.sp_energy,
        entry.composite_gibbs,
        entry.block.imaginary_count,
        result.lowest_freq_cm1,
        result.thermo_temperature_k,
        "; ".join(entry.block.warnings),
        population.cluster_key if population is not None else "",
        population.rel_e_kcalmol if population is not None else "",
        population.rel_g_kcalmol if population is not None else "",
        temperature if population is not None and population.population is not None else "",
        population.population if population is not None else "",
    ]


def render_workflow_si_csv(data: WorkflowSiData) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)
    # Populations align 1:1 with entries; extra blocks (unpaired SPs) are never
    # populated. An empty populations tuple means no minima → all-blank cells.
    populations = data.populations or ((None,) * len(data.entries))
    for entry, population in zip(data.entries, populations, strict=False):
        writer.writerow(_si_csv_row(entry, population, data.boltzmann_temperature_k))
    for entry in data.extra_blocks:
        writer.writerow(_si_csv_row(entry, None, data.boltzmann_temperature_k))
    return buffer.getvalue()


def _boltzmann_temperature_override(workspace_dir: Path) -> float | None:
    """Optional ``boltzmann_temperature_k`` from ``flow.yaml``; never raises.

    A malformed or missing manifest must not suppress the SI, so every failure
    (unreadable file, invalid YAML, non-mapping, non-positive value) logs and
    falls back to ``None`` — the parsed thermochemistry temperature is then used.
    """
    try:
        manifest = load_flow_manifest(workspace_dir)
    except (ValueError, OSError):
        logger.warning(
            "Ignoring flow manifest for Boltzmann temperature in %s", workspace_dir, exc_info=True
        )
        return None
    raw = manifest.get("boltzmann_temperature_k")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring non-numeric boltzmann_temperature_k=%r in %s", raw, workspace_dir)
        return None
    if not math.isfinite(value) or value <= 0:
        logger.warning("Ignoring non-positive boltzmann_temperature_k=%r in %s", raw, workspace_dir)
        return None
    return value


def write_workflow_si(workspace_dir: Path, payload: Mapping[str, Any]) -> Path | None:
    """Write ``workflow_si.md`` + ``si_data.csv``; never raises.

    A workflow without ORCA stages has no SI: stale files from an earlier
    template are removed so nothing obsolete can be pasted into a paper.
    """
    md_path = workspace_dir / WORKFLOW_SI_MD_FILE
    csv_path = workspace_dir / WORKFLOW_SI_CSV_FILE
    try:
        override = _boltzmann_temperature_override(workspace_dir)
        data = collect_workflow_si_data(payload, boltzmann_temperature_k=override)
        if not data.has_orca_stages():
            md_path.unlink(missing_ok=True)
            csv_path.unlink(missing_ok=True)
            return None
        # Render both documents before writing either, so a rendering failure
        # leaves the previous consistent pair on disk instead of a fresh md next
        # to a stale csv.
        md_text = render_workflow_si_md(data)
        csv_text = render_workflow_si_csv(data)
        atomic_write_text(md_path, md_text)
        atomic_write_text(csv_path, csv_text)
        return md_path
    except Exception:  # noqa: BLE001
        logger.warning("Workflow SI generation failed for %s", workspace_dir, exc_info=True)
        return None


__all__ = [
    "ExcludedStage",
    "PopulationRow",
    "WorkflowSiData",
    "WorkflowSiEntry",
    "collect_workflow_si_data",
    "render_workflow_si_csv",
    "render_workflow_si_md",
    "write_workflow_si",
]
