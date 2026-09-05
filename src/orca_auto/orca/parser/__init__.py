"""ORCA quantum chemistry output file (.out) parser.

Extracts key metadata such as energy, method, basis set, convergence status,
and coordinates from ORCA calculation results.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

from ..orca_chemistry import build_formula as _build_formula
from ..orca_opt_progress import OptProgress, OptStep, parse_opt_progress
from ..output_status import coarse_orca_status, last_optimization_convergence
from .extractors import (
    AtomRow,
)
from .extractors import (
    parse_coordinates as _parse_coordinates,
)
from .extractors import (
    parse_frequencies as _parse_frequencies,
)
from .extractors import (
    parse_input_line as _parse_input_line,
)
from .extractors import (
    parse_program_version as _parse_program_version,
)
from .extractors import (
    parse_solvation as _parse_solvation,
)
from .extractors import (
    parse_wall_time as _parse_wall_time,
)
from .io import read_orca_text as _read_orca_text
from .patterns import (
    _CHARGE_MULT_RE,
    _ENTHALPY_RE,
    _GIBBS_CORRECTION_RE,
    _GIBBS_RE,
    _THERMO_TEMPERATURE_RE,
    _ZPE_RE,
    FINAL_SINGLE_POINT_ENERGY_RE,
    final_single_point_energy_value,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HARTREE_TO_EV = 27.211386245988
# The one Hartree -> kcal/mol conversion factor. Report renderers and the
# workflow SI import this; do not re-declare it elsewhere.
KCAL_PER_HARTREE = 627.5094740631

__all__ = [
    "HARTREE_TO_EV",
    "KCAL_PER_HARTREE",
    "AtomRow",
    "OptProgress",
    "OptStep",
    "OrcaResult",
    "parse_opt_progress",
    "parse_orca_output",
]


# ---------------------------------------------------------------------------
# Result data class
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class OrcaResult:
    """Calculation results extracted from an ORCA output file."""

    source_path: str
    calc_type: str = ""
    method: str = ""
    basis_set: str = ""
    charge: int = 0
    multiplicity: int = 1
    formula: str = ""
    n_atoms: int = 0
    energy_hartree: float | None = None
    energy_ev: float | None = None
    energy_kcalmol: float | None = None
    opt_converged: bool | None = None
    has_imaginary_freq: bool | None = None
    lowest_freq_cm1: float | None = None
    enthalpy: float | None = None
    gibbs_energy: float | None = None
    zpe_correction: float | None = None
    gibbs_correction: float | None = None
    thermo_temperature_k: float | None = None
    wall_time_seconds: int | None = None
    status: str = "completed"
    file_hash: str = ""
    mtime: float = 0.0
    input_line: str = ""
    orca_version: str = ""
    solvation: str = ""
    elements: list[str] = field(default_factory=list)
    coordinates: list[AtomRow] = field(default_factory=list)
    # True only when charge/multiplicity were explicitly parsed from the
    # executed output.
    electronic_state_verified: bool = False


# ---------------------------------------------------------------------------
# Parser functions
# ---------------------------------------------------------------------------


def _compute_file_hash(file_path: str) -> str:
    """Return the first 16 characters of the file's SHA-256 hash."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def parse_orca_output(file_path: str) -> OrcaResult:
    """Parse an ORCA .out file and return an OrcaResult.

    Args:
        file_path: Path to the ORCA output file

    Returns:
        Extracted calculation results

    Raises:
        FileNotFoundError: If the file does not exist
        UnicodeDecodeError: If there is a file encoding issue
    """
    text = _read_orca_text(file_path)

    result = OrcaResult(source_path=file_path)
    result.mtime = os.path.getmtime(file_path)
    result.file_hash = _compute_file_hash(file_path)

    final_energy = _last_final_energy_match(text)

    _populate_input_metadata(result, text)
    _populate_coordinates(result, text)
    _populate_energy(result, final_energy)
    _populate_convergence(result, text)
    _populate_frequencies(result, text)
    _populate_thermodynamics(
        result,
        _final_stage_text(text, final_energy, published_energy=result.energy_hartree),
    )
    result.wall_time_seconds = _parse_wall_time(text)
    result.status = _parse_status(text, result)

    return result


def _populate_input_metadata(result: OrcaResult, text: str) -> None:
    calc_type, method, basis_set, input_tokens = _parse_input_line(text)
    result.calc_type = calc_type
    result.method = method
    result.basis_set = basis_set
    result.input_line = " ".join(input_tokens)
    result.orca_version = _parse_program_version(text)
    result.solvation = _parse_solvation(text, input_tokens)
    cm_match = _CHARGE_MULT_RE.search(text)
    if cm_match:
        result.charge = int(cm_match.group(1))
        result.multiplicity = int(cm_match.group(2))
        result.electronic_state_verified = True


def _populate_coordinates(result: OrcaResult, text: str) -> None:
    atoms = _parse_coordinates(text)
    result.coordinates = atoms
    result.elements = [atom[0] for atom in atoms]
    result.n_atoms = len(atoms)
    result.formula = _build_formula(result.elements)


def _last_final_energy_match(text: str) -> re.Match[str] | None:
    """Return the last final-energy line's match, or None when none is printed."""
    last_match: re.Match[str] | None = None
    for match in FINAL_SINGLE_POINT_ENERGY_RE.finditer(text):
        last_match = match
    return last_match


def _final_energy_line_annotated(final_energy: re.Match[str] | None) -> bool:
    """True when the last final-energy line carries an annotation.

    A trailing annotation such as "(SCF not fully converged!)" taints the
    final geometry's SCF: neither the energy nor thermochemistry derived from
    it may be published, and any earlier clean line belongs to a different
    geometry.
    """
    return final_energy is not None and final_energy.group(2) is not None


def _populate_energy(result: OrcaResult, final_energy: re.Match[str] | None) -> None:
    if final_energy is None or _final_energy_line_annotated(final_energy):
        return
    try:
        energy = final_single_point_energy_value(final_energy.group(1))
    except ValueError:
        return
    result.energy_hartree = energy
    result.energy_ev = energy * HARTREE_TO_EV
    result.energy_kcalmol = energy * KCAL_PER_HARTREE


def _populate_convergence(result: OrcaResult, text: str) -> None:
    result.opt_converged = last_optimization_convergence(text.splitlines())


def _final_stage_text(
    text: str,
    final_energy: re.Match[str] | None,
    *,
    published_energy: float | None,
) -> str | None:
    """Return the output printed after the published final energy, or None.

    ``published_energy`` is the value ``_populate_energy`` derived from
    ``final_energy``; it is None when that line was absent, unparseable or
    annotated, which is what makes the stage unpublishable.

    ORCA prints a full frequency/thermochemistry block for every Hessian it
    computes, so an optimization with ``Calc_Hess``/``Recalc_Hess`` carries
    several blocks and only the one after the last final single point energy
    describes the final geometry. Thermochemistry is read from that stage
    alone: earlier blocks are never published, and an output whose final stage
    has no block (an ``OptTS`` without ``Freq``) publishes none rather than an
    earlier geometry's values. Without a published final energy (none printed,
    unparseable, or annotated as an unconverged SCF) there is no stage to bind
    to and nothing is published. The imaginary-mode fields keep their
    whole-file last-block selection so that a failed run without a published
    final energy still reports its last frequency block.
    """
    if final_energy is None or published_energy is None:
        return None
    return text[final_energy.end() :]


def _populate_frequencies(result: OrcaResult, text: str) -> None:
    has_imag, lowest = _parse_frequencies(text)
    result.has_imaginary_freq = has_imag
    result.lowest_freq_cm1 = lowest


def _populate_thermodynamics(result: OrcaResult, stage_text: str | None) -> None:
    if stage_text is None:
        return
    enthalpy_match = _ENTHALPY_RE.search(stage_text)
    if enthalpy_match:
        result.enthalpy = float(enthalpy_match.group(1))
    gibbs_match = _GIBBS_RE.search(stage_text)
    if gibbs_match:
        result.gibbs_energy = float(gibbs_match.group(1))
    zpe_match = _ZPE_RE.search(stage_text)
    if zpe_match:
        result.zpe_correction = float(zpe_match.group(1))
    correction_match = _GIBBS_CORRECTION_RE.search(stage_text)
    if correction_match:
        result.gibbs_correction = float(correction_match.group(1))
    elif result.gibbs_energy is not None and result.energy_hartree is not None:
        # Outputs without a literal "G-E(el)" line still define the correction
        # exactly: both values refer to the final geometry, so their difference
        # IS G - E(el). Without this fallback an SP//opt workflow would
        # silently omit its composite G.
        result.gibbs_correction = result.gibbs_energy - result.energy_hartree
    temperature_match = _THERMO_TEMPERATURE_RE.search(stage_text)
    if temperature_match:
        result.thermo_temperature_k = float(temperature_match.group(1))


def _parse_status(text: str, result: OrcaResult) -> str:
    return coarse_orca_status(
        text,
        opt_converged=result.opt_converged,
        wall_time_seconds=result.wall_time_seconds,
    )
