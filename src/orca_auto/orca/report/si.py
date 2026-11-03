"""Copy-paste-ready SI (Supporting Information) block for one ORCA job.

``si_block.md`` holds the journal-standard per-structure record: route line
and program version, electronic energy, thermochemistry (ZPE / H / G and the
G-E(el) correction), the imaginary-mode summary, and the final Cartesian
coordinates — as plain fixed-width text that pastes cleanly into Word or a
LaTeX source. Lint warnings (``⚠`` lines) flag what a reviewer would: a
minimum with imaginary modes, a TS without exactly one, a constrained TS
search whose saddle is unverified, missing thermochemistry. Non-stationary relaxed scans still get no block; IRC gets a
summary-only validation block with no coordinates, also rendered here. This
module only renders; the publisher writes the block and records its outcome,
and like the HTML report a failure never breaks run finalization.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import SI_BLOCK_MD_FILE

from .. import evidence
from ..frequencies import ModeSummary, mode_summaries
from ..parser import OrcaResult
from ..route_rules import RouteFacts, route_facts
from ..statuses import RunStatus
from .irc import parse_irc_output
from .path import IrcPathPoint, path_endpoints, path_marker_point
from .settings import ReportSetting

logger = logging.getLogger(__name__)

# SI convention: energies in Eh to 6 decimals, coordinates in Å to 6 decimals.
_ENERGY_FMT = "{:16.6f}"
_MODE_TOP_ATOMS = 3


def _mode_note(summary: ModeSummary) -> str:
    # 1-based atom numbering: SI readers count atoms from 1, matching the
    # coordinate list below the header.
    atoms = "–".join(
        f"{entry.element}{entry.atom_index + 1}" for entry in summary.top_atoms[:_MODE_TOP_ATOMS]
    )
    note = f"ν‡ = {summary.frequency_cm:.1f} cm⁻¹"
    return f"{note}, {atoms} dominant" if atoms else note


def _lint_warnings(
    kind: str,
    result: OrcaResult,
    imaginary_count: int | None,
    geometry_scope: str | None = None,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if result.opt_converged is False:
        warnings.append("geometry optimization did NOT converge")
    if kind == "ts" and geometry_scope == "partial":
        # Still a TS record (route, Nimag, coordinates), but restricted
        # coordinates cannot establish a first-order saddle of the full surface.
        warnings.append(f"{evidence.CONSTRAINED_TS_SEARCH_NOTE} (geometry constraints applied)")
    if kind in ("min", "ts") and imaginary_count is None:
        warnings.append("no frequency calculation: stationary point is uncharacterized")
    if kind == "min" and imaginary_count is not None and imaginary_count > 0:
        warnings.append(f"expected a minimum but found {imaginary_count} imaginary mode(s)")
    if kind == "ts" and imaginary_count is not None and imaginary_count != 1:
        warnings.append(f"expected exactly 1 imaginary mode for a TS, found {imaginary_count}")
    if kind in ("min", "ts") and result.gibbs_energy is None and imaginary_count is not None:
        warnings.append("thermochemistry missing despite a frequency calculation")
    return tuple(warnings)


def render_si_block_md(block: evidence.OrcaStructureEvidence) -> str:
    """One structure's SI block as plain fixed-width text."""
    result = block.result
    lines = [f"== {block.name} =="]

    route = result.input_line or " ".join((result.method, result.basis_set)).strip()
    version_note = f"        (ORCA {result.orca_version})" if result.orca_version else ""
    lines.append(f"! {route}{version_note}")
    charge_line = (
        f"Charge {result.charge}, Multiplicity {result.multiplicity}"
        if result.electronic_state_verified
        else "Charge / multiplicity: unavailable"
    )
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

    warnings = _lint_warnings(block.kind, result, block.imaginary_count, block.geometry_scope)
    lines.extend(f"⚠ {warning}" for warning in warnings)

    # Multi-attempt runs keep several outputs (submitted and resumed outputs):
    # name the one these numbers came from, as the IRC block does.
    if block.last_out_name:
        lines.append(f"Last output: {block.last_out_name}")

    lines.extend(
        f"{element:<2}  {x:12.6f} {y:12.6f} {z:12.6f}" for element, x, y, z in result.coordinates
    )
    lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True)
class IrcSiBlock:
    name: str
    route_line: str
    orca_version: str
    settings: tuple[ReportSetting, ...]
    path_points: tuple[IrcPathPoint, ...]
    last_out_name: str


class IrcReportError(Exception):
    """The IRC SI block cannot be assembled because the completed run recorded no output."""


def collect_irc_si_block(
    reaction_dir: Path, state: Mapping[str, Any], route: RouteFacts
) -> IrcSiBlock | None:
    """The IRC validation block of an IRC route (``route.is_irc``); ``None`` until completed."""
    if str(state.get("status") or "") != RunStatus.COMPLETED.value:
        return None
    out_path = evidence.final_out_path(state)
    if out_path is None:
        raise IrcReportError(f"no output file found for {reaction_dir}")
    parsed = parse_irc_output(out_path)
    try:
        result, _analysis = evidence.parsed_final_output(out_path)
    except OSError:
        result = None
    return IrcSiBlock(
        name=reaction_dir.name,
        route_line=" ".join(route.route_lines),
        orca_version=result.orca_version if result is not None else "",
        settings=parsed.settings,
        path_points=parsed.path_points,
        last_out_name=out_path.name,
    )


def render_irc_si_block_md(block: IrcSiBlock) -> str:
    version_note = f"        (ORCA {block.orca_version})" if block.orca_version else ""
    lines = [
        f"== {block.name} ==",
        f"{block.route_line}{version_note}",
        "IRC validation summary",
    ]
    ts = path_marker_point(block.path_points, "TS")
    endpoint_1, endpoint_2 = path_endpoints(block.path_points)
    if block.path_points:
        lines.append(f"Parsed IRC path points = {len(block.path_points)}")
    if ts is not None:
        lines.append(
            f"TS step = {ts.label}; E = {ts.energy_hartree:.6f} Eh; "
            f"relative ΔE = {ts.relative_kcal:+.2f} kcal mol⁻¹"
        )
    for label, point in (("path endpoint 1", endpoint_1), ("path endpoint 2", endpoint_2)):
        if point is None:
            continue
        endpoint_text = (
            f"{label}: step {point.label}; E = {point.energy_hartree:.6f} Eh; "
            f"relative ΔE = {point.relative_kcal:+.2f} kcal mol⁻¹"
        )
        if ts is not None:
            endpoint_text += f"; from TS = {point.relative_kcal - ts.relative_kcal:+.2f} kcal mol⁻¹"
        lines.append(endpoint_text)

    trajectory_settings = [
        setting for setting in block.settings if "trajectory" in setting.label.lower()
    ]
    for setting in trajectory_settings:
        lines.append(f"{setting.label}: {setting.value}")
    if block.last_out_name:
        lines.append(f"Last output: {block.last_out_name}")
    lines.append(
        "⚠ IRC endpoints are path endpoints, not fully optimized stationary structures; "
        "optimize endpoints before publishing endpoint coordinates."
    )
    lines.append("")
    return "\n".join(lines)


def si_block_path(reaction_dir: Path) -> Path:
    return reaction_dir / SI_BLOCK_MD_FILE


def render_si_block(
    reaction_dir: Path, state: Mapping[str, Any], route: RouteFacts | None = None
) -> str | None:
    """The ``si_block.md`` text of the job; ``None`` when the job has no SI block.

    ``route`` is the publication's route facts of ``state["selected_inp"]``;
    without it they are read here. Writing the block, removing a stale one and
    recording the outcome are the publisher's
    (``report.publication.write_si_block``).
    """
    selected_raw = str(state.get("selected_inp") or "").strip()
    if not selected_raw:
        return None
    if route is None:
        route = route_facts(Path(selected_raw))
    if route.is_irc:
        irc_block = collect_irc_si_block(reaction_dir, state, route)
        return None if irc_block is None else render_irc_si_block_md(irc_block)
    block = evidence.collect_structure_evidence(reaction_dir, state, route)
    return None if block is None else render_si_block_md(block)


__all__ = [
    "IrcReportError",
    "IrcSiBlock",
    "collect_irc_si_block",
    "render_irc_si_block_md",
    "render_si_block",
    "render_si_block_md",
    "si_block_path",
]
