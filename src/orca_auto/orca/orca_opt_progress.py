"""Optimization progress parsing for ORCA output files."""

from __future__ import annotations

from dataclasses import dataclass, field

from .orca_chemistry import build_formula
from .output_status import last_optimization_convergence
from .parser.extractors import (
    parse_coordinates,
    parse_input_line,
    parse_optimization_cycles,
)
from .parser.io import read_orca_text


@dataclass
class OptStep:
    """Energy for a single optimization cycle."""

    cycle: int
    energy_hartree: float


@dataclass
class OptProgress:
    """Summary of optimization progress."""

    source_path: str
    formula: str = ""
    method: str = ""
    basis_set: str = ""
    steps: list[OptStep] = field(default_factory=list)
    is_converged: bool = False


def parse_opt_progress(file_path: str) -> OptProgress:
    """Extract per-cycle energy/convergence data from an ORCA optimization output.

    Args:
        file_path: Path to the ORCA output file

    Returns:
        Optimization progress summary

    Raises:
        FileNotFoundError: If the file does not exist
    """
    return parse_opt_progress_text(read_orca_text(file_path), source_path=file_path)


def parse_opt_progress_text(text: str, *, source_path: str) -> OptProgress:
    """Extract optimization progress from already-decoded output text."""

    method, basis_set, _ = parse_input_line(text)
    elements = [atom[0] for atom in parse_coordinates(text)]
    formula = build_formula(elements)

    progress = OptProgress(
        source_path=source_path,
        formula=formula,
        method=method,
        basis_set=basis_set,
        is_converged=last_optimization_convergence(text.splitlines()) is True,
    )

    for cycle_num, energy, _ in parse_optimization_cycles(text):
        if energy is None:
            continue

        progress.steps.append(OptStep(cycle=cycle_num, energy_hartree=energy))

    return progress
