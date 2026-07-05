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
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import WORKFLOW_SI_CSV_FILE, WORKFLOW_SI_MD_FILE
from orca_auto.core.utils.persistence import atomic_write_text
from orca_auto.orca.report.render import KCAL_PER_HARTREE
from orca_auto.orca.report.si import (
    SiBlock,
    SiBlockError,
    collect_si_block,
    render_si_block_md,
)
from orca_auto.orca.state import load_state

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


def collect_workflow_si_data(payload: Mapping[str, Any]) -> WorkflowSiData:
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
    )


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


def _distinct_levels(entries: tuple[WorkflowSiEntry, ...]) -> list[tuple[str, str, str, str]]:
    seen: list[tuple[str, str, str, str]] = []
    for entry in entries:
        key = _level_key(entry.block)
        if key not in seen:
            seen.append(key)
    return seen


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
    return (
        "Electronic energies were refined by single-point calculations on the optimized "
        f"geometries {phrase}; composite Gibbs energies combine E(SP) with the G − E(el) "
        "correction from the optimization level."
    )


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
    for method, basis, solvation, version in _distinct_levels(data.entries):
        lines.append(
            "Geometry optimizations and harmonic frequency calculations were performed "
            f"{_level_phrase(method, basis, solvation, version)}."
        )
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
    rows = [
        (entry, energy)
        for entry in data.entries
        for energy in (entry.block.result.energy_hartree,)
        if energy is not None
    ]
    if not rows:
        return ["(no completed stationary structures yet)"]

    entries = [entry for entry, _ in rows]
    all_composite = all(entry.composite_gibbs is not None for entry in entries)

    def gibbs_of(entry: WorkflowSiEntry) -> float | None:
        return entry.composite_gibbs if all_composite else entry.block.result.gibbs_energy

    best_e = min(energy for _, energy in rows)
    gibbs_values = [g for g in (gibbs_of(entry) for entry in entries) if g is not None]
    best_g = min(gibbs_values) if gibbs_values else None

    name_width = max(9, *(len(entry.block.name) for entry in entries))
    header = (
        f"{'#':>2}  {'structure':<{name_width}}  {'E(el)/Eh':>14}  "
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
    if all_composite:
        lines.append("")
        lines.append(
            "G is the composite G = E(SP) + [G − E(el)](opt level); "
            "single points paired by identical geometry."
        )
    elif any(entry.composite_gibbs is not None for entry in entries):
        lines.append("")
        lines.append(
            "⚠ composite G available for only part of the set; "
            "ΔG uses the optimization-level G for all entries to keep one baseline."
        )
    return lines


# ---------------------------------------------------------------------------
# Rendering / writing
# ---------------------------------------------------------------------------


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
]


def render_workflow_si_csv(data: WorkflowSiData) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)
    for entry in (*data.entries, *data.extra_blocks):
        result = entry.block.result
        sp = entry.sp_block.result if entry.sp_block is not None else None
        writer.writerow(
            [
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
            ]
        )
    return buffer.getvalue()


def write_workflow_si(workspace_dir: Path, payload: Mapping[str, Any]) -> Path | None:
    """Write ``workflow_si.md`` + ``si_data.csv``; never raises.

    A workflow without ORCA stages has no SI: stale files from an earlier
    template are removed so nothing obsolete can be pasted into a paper.
    """
    md_path = workspace_dir / WORKFLOW_SI_MD_FILE
    csv_path = workspace_dir / WORKFLOW_SI_CSV_FILE
    try:
        data = collect_workflow_si_data(payload)
        if not data.has_orca_stages():
            md_path.unlink(missing_ok=True)
            csv_path.unlink(missing_ok=True)
            return None
        atomic_write_text(md_path, render_workflow_si_md(data))
        atomic_write_text(csv_path, render_workflow_si_csv(data))
        return md_path
    except Exception:  # noqa: BLE001
        logger.warning("Workflow SI generation failed for %s", workspace_dir, exc_info=True)
        return None


__all__ = [
    "ExcludedStage",
    "WorkflowSiData",
    "WorkflowSiEntry",
    "collect_workflow_si_data",
    "render_workflow_si_csv",
    "render_workflow_si_md",
    "write_workflow_si",
]
