"""Pure Markdown rendering for workflow Supporting Information."""

from __future__ import annotations

import math

from orca_auto.orca.evidence import (
    OrcaStructureEvidence,
)
from orca_auto.orca.parser import KCAL_PER_HARTREE
from orca_auto.orca.report.si import (
    render_si_block_md,
)

from ...conformer_selection import finite
from .collection import (
    _POP_NO_GIBBS_NOTE,
    PopulationRow,
    WorkflowSiData,
    WorkflowSiEntry,
    _energy_convention,
    _has_complete_comparison_provenance,
    _provenance_key,
)

# ---------------------------------------------------------------------------
# Methods paragraph
# ---------------------------------------------------------------------------


def _level_key(block: OrcaStructureEvidence) -> tuple[str, str, str, str]:
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


def _documented_blocks(data: WorkflowSiData) -> list[OrcaStructureEvidence]:
    """Every block whose level the SI must document, including matched SPs."""
    blocks: list[OrcaStructureEvidence] = []
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
    provenance_items = [
        (
            (entry.sp_block, entry.sp_selected_input_identity)
            if convention.use_single_point_energy
            else (entry.block, entry.selected_input_identity)
        )
        for entry in candidates
    ]
    known_levels = {
        _provenance_key(block, selected_input_identity)
        for block, selected_input_identity in provenance_items
        if block is not None
        and selected_input_identity is not None
        and _has_complete_comparison_provenance(block, selected_input_identity)
    }
    complete_provenance = all(
        _has_complete_comparison_provenance(block, selected_input_identity)
        for block, selected_input_identity in provenance_items
    )
    if len(candidates) > 1 and (not complete_provenance or len(known_levels) != 1):
        return [
            "(relative energies omitted: executed route/electronic-state provenance "
            "is missing or differs across completed structures)"
        ]

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
                f"Minima are RMSD representatives; {merged} degenerate minima were merged."
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
