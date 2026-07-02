"""ORCA quantum chemistry output file (.out) parser.

Extracts key metadata such as energy, method, basis set, convergence status,
and coordinates from ORCA calculation results.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from ..orca_chemistry import build_formula as _build_formula
from ..orca_opt_progress import OptProgress, OptStep, parse_opt_progress
from ..output_status import coarse_orca_status
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
    parse_wall_time as _parse_wall_time,
)
from .io import read_orca_text as _read_orca_text
from .patterns import (
    _CHARGE_MULT_RE,
    _ENERGY_RE,
    _ENTHALPY_RE,
    _GIBBS_RE,
    _OPT_CONVERGED_RE,
    _OPT_NOT_CONVERGED_RE,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HARTREE_TO_EV = 27.211386245988
HARTREE_TO_KCALMOL = 627.5094740631

__all__ = [
    "HARTREE_TO_EV",
    "HARTREE_TO_KCALMOL",
    "OptProgress",
    "OptStep",
    "OrcaResult",
    "parse_opt_progress",
    "parse_orca_output",
]


# ---------------------------------------------------------------------------
# Result data class
# ---------------------------------------------------------------------------


@dataclass
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
    wall_time_seconds: int | None = None
    status: str = "completed"
    file_hash: str = ""
    mtime: float = 0.0
    input_line: str = ""
    elements: list[str] = field(default_factory=list)


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

    _populate_input_metadata(result, text)
    _populate_coordinates(result, text)
    _populate_energy(result, text)
    _populate_convergence(result, text)
    _populate_frequencies(result, text)
    _populate_thermodynamics(result, text)
    result.wall_time_seconds = _parse_wall_time(text)
    result.status = _parse_status(text, result)

    return result


def _populate_input_metadata(result: OrcaResult, text: str) -> None:
    calc_type, method, basis_set, input_tokens = _parse_input_line(text)
    result.calc_type = calc_type
    result.method = method
    result.basis_set = basis_set
    result.input_line = " ".join(input_tokens)
    cm_match = _CHARGE_MULT_RE.search(text)
    if cm_match:
        result.charge = int(cm_match.group(1))
        result.multiplicity = int(cm_match.group(2))


def _populate_coordinates(result: OrcaResult, text: str) -> None:
    elements, n_atoms = _parse_coordinates(text)
    result.elements = elements
    result.n_atoms = n_atoms
    result.formula = _build_formula(elements)


def _populate_energy(result: OrcaResult, text: str) -> None:
    energy_matches = _ENERGY_RE.findall(text)
    if not energy_matches:
        return
    energy = float(energy_matches[-1])
    result.energy_hartree = energy
    result.energy_ev = energy * HARTREE_TO_EV
    result.energy_kcalmol = energy * HARTREE_TO_KCALMOL


def _populate_convergence(result: OrcaResult, text: str) -> None:
    if _OPT_CONVERGED_RE.search(text):
        result.opt_converged = True
    elif _OPT_NOT_CONVERGED_RE.search(text):
        result.opt_converged = False


def _populate_frequencies(result: OrcaResult, text: str) -> None:
    has_imag, lowest = _parse_frequencies(text)
    result.has_imaginary_freq = has_imag
    result.lowest_freq_cm1 = lowest


def _populate_thermodynamics(result: OrcaResult, text: str) -> None:
    enthalpy_match = _ENTHALPY_RE.search(text)
    if enthalpy_match:
        result.enthalpy = float(enthalpy_match.group(1))
    gibbs_match = _GIBBS_RE.search(text)
    if gibbs_match:
        result.gibbs_energy = float(gibbs_match.group(1))


def _parse_status(text: str, result: OrcaResult) -> str:
    return coarse_orca_status(
        text,
        opt_converged=result.opt_converged,
        wall_time_seconds=result.wall_time_seconds,
    )
