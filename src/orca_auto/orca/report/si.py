"""Copy-paste-ready SI (Supporting Information) block for one ORCA job.

``si_block.md`` holds the journal-standard per-structure record: route line
and program version, electronic energy, thermochemistry (ZPE / H / G and the
G-E(el) correction), the imaginary-mode summary, and the final Cartesian
coordinates — as plain fixed-width text that pastes cleanly into Word or a
LaTeX source. Lint warnings (``⚠`` lines) flag what a reviewer would: a
minimum with imaginary modes, a TS without exactly one, missing
thermochemistry. Non-stationary relaxed scans still get no block; IRC gets a
summary-only validation block with no coordinates. ScanTS does get a structure
block, because ORCA finishes it with an internal OptTS so the final geometry is
a genuine TS. Like the HTML report, generation must never break run
finalization: every error is logged and swallowed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import SI_BLOCK_MD_FILE
from orca_auto.core.utils.persistence import atomic_write_text

from ..completion_rules import IRC_ROUTE_RE, OPT_ROUTE_RE, TS_ROUTE_RE
from ..input_blocks import file_route_lines
from ..parser import OrcaResult, parse_orca_output
from ..scants import first_scan_coordinate_spec
from .attempts import final_out_path
from .frequencies import (
    FrequencyAnalysis,
    ModeSummary,
    mode_summaries,
    parse_frequency_analysis,
)
from .irc import collect_irc_si_block, input_uses_irc, render_irc_si_block_md

logger = logging.getLogger(__name__)

# Route families whose final geometry is not a stationary point: path methods
# (plain NEB / NEB-CI — NEB-TS is claimed by the TS check first) and dynamics.
# Their endpoints must never be published as structures in an SI. No SCAN
# token here: `SCAN` in a route line is the density functional
# (`! SCAN def2-SVP Opt Freq`), not a scan job — relaxed scans are identified
# from the `%geom Scan` block and ScanTS by the TS check.
_NON_STATIONARY_ROUTE_RE = re.compile(
    r"\b(?:ZOOM-)?NEB(?:-CI)?\b|\bMD\b",
    re.IGNORECASE,
)

# SI convention: energies in Eh to 6 decimals, coordinates in Å to 6 decimals.
_ENERGY_FMT = "{:16.6f}"
_MODE_TOP_ATOMS = 3


class SiBlockError(Exception):
    """The job has no valid SI block (missing output, energy, or geometry)."""


def structure_kind(selected_inp: Path) -> str | None:
    """``"ts"`` / ``"min"`` / ``"sp"``; ``None`` for non-stationary jobs.

    A plain relaxed scan (Opt route + scan coordinate), IRC, plain NEB paths,
    and MD end on non-stationary geometries that must never enter the
    stationary-structure SI path; IRC has a separate summary-only writer. TS
    routes (OptTS/ScanTS/NEB-TS) and plain Opt end on stationary points.
    Everything else (single points, bare Freq) is reported without a minimum/TS
    claim.
    """
    routes = " ".join(file_route_lines(selected_inp))
    if not routes:
        return None
    if IRC_ROUTE_RE.search(routes):
        return None
    if TS_ROUTE_RE.search(routes):
        return "ts"
    if _NON_STATIONARY_ROUTE_RE.search(routes):
        return None
    if OPT_ROUTE_RE.search(routes):
        if first_scan_coordinate_spec(selected_inp) is not None:
            return None
        return "min"
    return "sp"


@lru_cache(maxsize=32)
def _parsed_output_cached(
    out_path_text: str, mtime_ns: int, size: int
) -> tuple[OrcaResult, FrequencyAnalysis | None]:
    return (
        parse_orca_output(out_path_text),
        parse_frequency_analysis(Path(out_path_text)),
    )


def parsed_final_output(out_path: Path) -> tuple[OrcaResult, FrequencyAnalysis | None]:
    """Parsed (result, frequency analysis), cached per (path, mtime, size).

    Workflow SI regeneration re-reads every completed stage on every advance;
    a finished job's output never changes, so parsing it once per process is
    enough. Callers must treat both returned objects as read-only — they are
    shared across cache hits.
    """
    stat = out_path.stat()
    return _parsed_output_cached(str(out_path), stat.st_mtime_ns, stat.st_size)


def _mode_note(summary: ModeSummary) -> str:
    # 1-based atom numbering: SI readers count atoms from 1, matching the
    # coordinate list below the header.
    atoms = "–".join(
        f"{entry.element}{entry.atom_index + 1}" for entry in summary.top_atoms[:_MODE_TOP_ATOMS]
    )
    note = f"ν‡ = {summary.frequency_cm:.1f} cm⁻¹"
    return f"{note}, {atoms} dominant" if atoms else note


def _lint_warnings(kind: str, result: OrcaResult, imaginary_count: int | None) -> tuple[str, ...]:
    warnings: list[str] = []
    if result.opt_converged is False:
        warnings.append("geometry optimization did NOT converge")
    if kind in ("min", "ts") and imaginary_count is None:
        warnings.append("no frequency calculation: stationary point is uncharacterized")
    if kind == "min" and imaginary_count is not None and imaginary_count > 0:
        warnings.append(f"expected a minimum but found {imaginary_count} imaginary mode(s)")
    if kind == "ts" and imaginary_count is not None and imaginary_count != 1:
        warnings.append(f"expected exactly 1 imaginary mode for a TS, found {imaginary_count}")
    if kind in ("min", "ts") and result.gibbs_energy is None and imaginary_count is not None:
        warnings.append("thermochemistry missing despite a frequency calculation")
    return tuple(warnings)


@dataclass(frozen=True)
class SiBlock:
    """Everything needed to render one structure's SI block."""

    name: str
    kind: str
    result: OrcaResult
    analysis: FrequencyAnalysis | None
    imaginary_count: int | None
    warnings: tuple[str, ...]


def collect_si_block(reaction_dir: Path, state: Mapping[str, Any]) -> SiBlock | None:
    """SI block for a completed job; ``None`` when the job type has none.

    Raises:
        SiBlockError: for a job that should have a block but is missing its
            output, final energy, or coordinates.
    """
    if str(state.get("status") or "") != "completed":
        return None
    selected_raw = str(state.get("selected_inp") or "").strip()
    if not selected_raw:
        return None
    selected_inp = Path(selected_raw)
    # An unreadable input is an error, not "this job type has no SI block":
    # every valid ORCA input has at least one route line, so an empty read
    # means the file is gone (archived / moved stage dir).
    if not file_route_lines(selected_inp):
        raise SiBlockError(f"cannot read route lines from input {selected_inp}")
    kind = structure_kind(selected_inp)
    if kind is None:
        return None

    out_path = final_out_path(state)
    if out_path is None:
        raise SiBlockError(f"no output file found for {reaction_dir}")
    result, analysis = parsed_final_output(out_path)
    if result.energy_hartree is None or not result.coordinates:
        raise SiBlockError(f"output {out_path} lacks a final energy or geometry")

    imaginary_count = analysis.imaginary_count() if analysis is not None else None
    return SiBlock(
        name=reaction_dir.name,
        kind=kind,
        result=result,
        analysis=analysis,
        imaginary_count=imaginary_count,
        warnings=_lint_warnings(kind, result, imaginary_count),
    )


def render_si_block_md(block: SiBlock) -> str:
    """One structure's SI block as plain fixed-width text."""
    result = block.result
    lines = [f"== {block.name} =="]

    route = (
        result.input_line or " ".join((result.method, result.basis_set, result.calc_type)).strip()
    )
    version_note = f"        (ORCA {result.orca_version})" if result.orca_version else ""
    lines.append(f"! {route}{version_note}")
    charge_line = f"Charge {result.charge}, Multiplicity {result.multiplicity}"
    if result.formula:
        charge_line += f"  ({result.formula})"
    lines.append(charge_line)

    # Label H/G with the temperature only when the output stated one; an SI
    # must not assert 298.15 K for a run whose thermochemistry line was not
    # parsed — the job may have used %freq Temp.
    temp = result.thermo_temperature_k
    temp_label = f" ({temp:.2f} K)" if temp is not None else ""
    rows: list[tuple[str, float | None]] = [
        ("E(el)", result.energy_hartree),
        ("ZPE correction", result.zpe_correction),
        (f"H{temp_label}", result.enthalpy),
        (f"G{temp_label}", result.gibbs_energy),
        ("G-E(el)", result.gibbs_correction),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        if value is None:
            continue
        lines.append(f"{label:<{width}} = {_ENERGY_FMT.format(value)} Eh")

    if block.imaginary_count is not None:
        nimag_line = f"Nimag = {block.imaginary_count}"
        if block.analysis is not None and block.imaginary_count > 0:
            notes = [
                _mode_note(summary)
                for summary in mode_summaries(block.analysis, None)
                if summary.imaginary
            ]
            if notes:
                nimag_line += f"  ({'; '.join(notes)})"
        lines.append(nimag_line)

    lines.extend(f"⚠ {warning}" for warning in block.warnings)

    lines.extend(
        f"{element:<2}  {x:12.6f} {y:12.6f} {z:12.6f}" for element, x, y, z in result.coordinates
    )
    lines.append("")
    return "\n".join(lines)


def si_block_path(reaction_dir: Path) -> Path:
    return reaction_dir / SI_BLOCK_MD_FILE


def write_si_block(reaction_dir: Path, state: Mapping[str, Any]) -> Path | None:
    """Write ``si_block.md``; ``None`` when the job has no SI block.

    Mirrors ``write_job_html_report``: a job type without a block removes any
    stale file from a reused reaction dir, while an unexpected error leaves
    the last valid block in place.
    """
    path = si_block_path(reaction_dir)
    try:
        selected_raw = str(state.get("selected_inp") or "").strip()
        if selected_raw and input_uses_irc(Path(selected_raw)):
            irc_block = collect_irc_si_block(reaction_dir, state)
            if irc_block is None:
                path.unlink(missing_ok=True)
                return None
            atomic_write_text(path, render_irc_si_block_md(irc_block))
            return path

        block = collect_si_block(reaction_dir, state)
        if block is None:
            path.unlink(missing_ok=True)
            return None
        atomic_write_text(path, render_si_block_md(block))
        return path
    except Exception:  # noqa: BLE001
        logger.warning("SI block generation failed for %s", reaction_dir, exc_info=True)
        return None


__all__ = [
    "SiBlock",
    "SiBlockError",
    "collect_si_block",
    "parsed_final_output",
    "render_si_block_md",
    "si_block_path",
    "structure_kind",
    "write_si_block",
]
