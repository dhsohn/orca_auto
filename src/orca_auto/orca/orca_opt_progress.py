"""Optimization progress parsing for ORCA output files."""

from __future__ import annotations

from dataclasses import dataclass, field

from .orca_chemistry import build_formula
from .output_status import has_error_termination, has_normal_termination
from .parser.extractors import parse_coordinates, parse_input_line, parse_wall_time
from .parser.io import read_orca_text
from .parser.patterns import (
    _CONVERGENCE_ITEM_RE,
    _ENERGY_RE,
    _OPT_CONVERGED_RE,
    _OPT_CYCLE_RE,
)


@dataclass
class OptStep:
    """Convergence data for a single optimization cycle."""

    cycle: int
    energy_hartree: float
    energy_change: float | None = None
    max_gradient: float | None = None
    rms_gradient: float | None = None
    max_step: float | None = None
    rms_step: float | None = None
    converged_flags: dict[str, bool] = field(default_factory=dict)


@dataclass
class OptProgress:
    """Summary of optimization progress."""

    source_path: str
    formula: str = ""
    method: str = ""
    basis_set: str = ""
    calc_type: str = ""
    steps: list[OptStep] = field(default_factory=list)
    is_converged: bool = False
    is_running: bool = False


def parse_opt_progress(file_path: str) -> OptProgress:
    """Extract per-cycle energy/convergence data from an ORCA optimization output.

    Args:
        file_path: Path to the ORCA output file

    Returns:
        Optimization progress summary

    Raises:
        FileNotFoundError: If the file does not exist
    """
    text = read_orca_text(file_path)

    calc_type, method, basis_set, _ = parse_input_line(text)
    elements, _ = parse_coordinates(text)
    formula = build_formula(elements)

    progress = OptProgress(
        source_path=file_path,
        formula=formula,
        method=method,
        basis_set=basis_set,
        calc_type=calc_type,
    )

    cycle_positions = [(m.start(), int(m.group(1))) for m in _OPT_CYCLE_RE.finditer(text)]
    if not cycle_positions:
        return progress

    energy_positions = [(m.start(), float(m.group(1))) for m in _ENERGY_RE.finditer(text)]

    for i, (cycle_start, cycle_num) in enumerate(cycle_positions):
        cycle_end = cycle_positions[i + 1][0] if i + 1 < len(cycle_positions) else len(text)
        cycle_text = text[cycle_start:cycle_end]

        energy: float | None = None
        for epos, eval_ in energy_positions:
            if cycle_start <= epos < cycle_end:
                energy = eval_

        if energy is None:
            continue

        progress.steps.append(_parse_opt_step(cycle_num, energy, cycle_text))

    progress.is_converged = bool(_OPT_CONVERGED_RE.search(text))
    progress.is_running = (
        not has_normal_termination(text)
        and not has_error_termination(text)
        and parse_wall_time(text) is None
    )

    return progress


_OPT_STEP_ATTRS = {
    "Energy change": "energy_change",
    "MAX gradient": "max_gradient",
    "RMS gradient": "rms_gradient",
    "MAX step": "max_step",
    "RMS step": "rms_step",
}


def _parse_opt_step(cycle_num: int, energy: float, cycle_text: str) -> OptStep:
    step = OptStep(cycle=cycle_num, energy_hartree=energy)
    for item_match in _CONVERGENCE_ITEM_RE.finditer(cycle_text):
        name = item_match.group(1)
        value = float(item_match.group(2))
        step.converged_flags[name] = item_match.group(3) == "YES"
        attr_name = _OPT_STEP_ATTRS.get(name)
        if attr_name is not None:
            setattr(step, attr_name, value)
    return step
