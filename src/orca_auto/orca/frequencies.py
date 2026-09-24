"""Frequency, normal-mode, and geometry parsing plus vibrational summaries.

The parser streams an ORCA output once and keeps the LAST ``VIBRATIONAL
FREQUENCIES`` / ``NORMAL MODES`` / ``CARTESIAN COORDINATES`` blocks. A
frequency or mode block that is followed by another ``FINAL SINGLE POINT
ENERGY`` line belongs to an earlier geometry (the initial or a recalculated
Hessian of an optimization) and is discarded, so only a frequency calculation
at the final geometry is reported. Summaries condense a mode into its dominant
atom displacements and, when a bond pair is given, its alignment with that
coordinate.

This module is the single source of truth for which frequency section counts,
which lines of it are frequencies, and which of those are imaginary. The
completion analyzer (``out_analyzer``) verifies a TS through
:func:`scan_frequency_sections` and :meth:`FrequencyAnalysis.imaginary_count`,
the same calls the SI and reports publish from, so a verified TS can never be
re-counted differently downstream (``docs/PUBLIC_CONTRACTS.md`` §4). Lines are
split like a file read (CR, LF, CRLF only) and input echoes and comments are
skipped, as every other execution diagnostic does.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .completion_rules import IMAGINARY_FREQ_THRESHOLD_CM1
from .output_status import is_execution_output_line, iter_output_lines
from .parser.io import read_orca_text

logger = logging.getLogger(__name__)

# Same noise cutoff the completion analyzer applies: the SI and reports must
# count a verified TS's modes exactly as the verifier did, or the pipeline
# publishes a Nimag that contradicts its own COMPLETED verdict.
FREQ_EPS_CM = IMAGINARY_FREQ_THRESHOLD_CM1
_TOP_ATOM_COUNT = 5

_FREQ_HEADER = "VIBRATIONAL FREQUENCIES"
_FINAL_ENERGY_HEADER = "FINAL SINGLE POINT ENERGY"
_MODES_HEADER = "NORMAL MODES"
_COORDS_HEADER = "CARTESIAN COORDINATES (ANGSTROEM)"
# One printed wavenumber: a signed number followed by ``cm**-1``, preceded by
# the line start, whitespace, or the mode index's colon (``   6:  -412.34 cm**-1``).
# This is the rule the completion analyzer has always verified a TS by; it
# also accepts the numbered form ORCA prints.
FREQUENCY_VALUE_RE = re.compile(r"(?:^|[\s:])(-?\d+(?:\.\d+)?)\s*cm\*\*-1", re.IGNORECASE)
_COORD_LINE_RE = re.compile(r"^\s*([A-Za-z]{1,2})\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s*$")


@dataclass(frozen=True)
class FrequencyAnalysis:
    frequencies: tuple[float, ...]
    mode_matrix: dict[int, dict[int, float]]
    atoms: tuple[tuple[str, float, float, float], ...]

    def mode_vector(self, mode_index: int) -> list[float]:
        n_rows = 3 * len(self.atoms)
        return [self.mode_matrix.get(row, {}).get(mode_index, 0.0) for row in range(n_rows)]

    def imaginary_count(self) -> int:
        """Number of modes below ``-IMAGINARY_FREQ_THRESHOLD_CM1``; the published Nimag."""
        return sum(1 for freq in self.frequencies if is_imaginary_frequency(freq))


@dataclass(frozen=True)
class FrequencySections:
    """Outcome of one pass over an output's ``VIBRATIONAL FREQUENCIES`` sections.

    ``analysis`` is the section (with its modes and geometry) that follows the
    last final single point energy, or ``None``. ``seen`` is True when any
    section header was read at all: with ``analysis`` None that means every
    section was superseded by a later final energy (or printed no
    frequencies), which is not the same as an output that never ran a
    frequency calculation.
    """

    analysis: FrequencyAnalysis | None
    seen: bool


def is_imaginary_frequency(frequency_cm: float) -> bool:
    """True below the shared noise threshold; ``-5 cm**-1`` is noise, not a mode."""
    return frequency_cm < -IMAGINARY_FREQ_THRESHOLD_CM1


def frequency_values(line: str) -> list[float]:
    """Wavenumbers printed on one output line; empty for any other line."""
    return [float(match.group(1)) for match in FREQUENCY_VALUE_RE.finditer(line)]


@dataclass(frozen=True)
class ModeAtomDisplacement:
    atom_index: int
    element: str
    displacement: float


@dataclass(frozen=True)
class ModeSummary:
    mode_index: int
    frequency_cm: float
    imaginary: bool
    top_atoms: tuple[ModeAtomDisplacement, ...]
    scan_alignment: float | None


def parse_frequency_analysis(out_path: Path) -> FrequencyAnalysis | None:
    """Last frequency/mode/geometry blocks of one out file; ``None`` without freqs."""
    try:
        # Use the same ORCA-aware decoding as the result parser, including UTF-16.
        text = read_orca_text(str(out_path))
    except OSError:
        return None
    return parse_frequency_analysis_text(text)


def parse_frequency_analysis_text(text: str) -> FrequencyAnalysis | None:
    """Last final-geometry frequency/mode/geometry blocks of decoded ORCA output."""
    return scan_frequency_sections(iter_output_lines(text)).analysis


def scan_frequency_sections(lines: Iterable[str]) -> FrequencySections:
    """Single pass over output lines (a file handle or :func:`iter_output_lines`).

    Only CR, LF and CRLF may separate the lines fed here; ``str.splitlines()``
    also breaks on form feeds and Unicode separators and would section a small
    output differently from the same output streamed from disk.
    """
    freqs: list[float] | None = None
    seen = False
    modes: dict[int, dict[int, float]] | None = None
    coords: list[tuple[str, float, float, float]] | None = None

    section = ""
    started = False
    current_freqs: list[float] = []
    current_modes: dict[int, dict[int, float]] = {}
    current_cols: list[int] = []
    current_coords: list[tuple[str, float, float, float]] = []

    def close_section() -> None:
        nonlocal freqs, modes, coords, section, started
        if section == "freq" and current_freqs:
            freqs = list(current_freqs)
        elif section == "modes" and current_modes:
            modes = {row: dict(cols) for row, cols in current_modes.items()}
        elif section == "coords" and current_coords:
            coords = list(current_coords)
        section = ""
        started = False

    for line in lines:
        if not is_execution_output_line(line):
            continue
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith(_FINAL_ENERGY_HEADER):
            # A later final energy supersedes every frequency block before it;
            # the final geometry's coordinates are printed before this line and
            # are kept.
            close_section()
            freqs = None
            modes = None
            continue
        if upper == _FREQ_HEADER:
            close_section()
            section, current_freqs = "freq", []
            seen = True
            continue
        if upper == _MODES_HEADER:
            close_section()
            section, current_modes, current_cols = "modes", {}, []
            continue
        if upper.startswith(_COORDS_HEADER):
            close_section()
            section, current_coords = "coords", []
            continue
        if section == "freq":
            values = frequency_values(line)
            if values:
                current_freqs.extend(values)
                started = True
            elif stripped and started:
                close_section()
        elif section == "coords":
            match = _COORD_LINE_RE.match(line)
            if match is not None:
                current_coords.append(
                    (
                        match.group(1),
                        float(match.group(2)),
                        float(match.group(3)),
                        float(match.group(4)),
                    )
                )
                started = True
            elif started:
                close_section()
        elif section == "modes":
            if not stripped:
                continue
            if not _consume_modes_line(stripped, current_modes, current_cols):
                if started:
                    close_section()
            else:
                started = True
    close_section()

    if freqs is None:
        return FrequencySections(analysis=None, seen=seen)
    return FrequencySections(
        analysis=FrequencyAnalysis(
            frequencies=tuple(freqs),
            mode_matrix=modes or {},
            atoms=tuple(coords or []),
        ),
        seen=seen,
    )


def _consume_modes_line(
    stripped: str,
    matrix: dict[int, dict[int, float]],
    cols: list[int],
) -> bool:
    tokens = stripped.split()
    try:
        int_tokens = [int(token) for token in tokens]
    except ValueError:
        int_tokens = None
    if int_tokens is not None:
        cols[:] = int_tokens
        return True
    if not cols:
        return False
    try:
        row = int(tokens[0])
        values = [float(token) for token in tokens[1:]]
    except (ValueError, IndexError):
        return False
    if len(values) != len(cols):
        return False
    row_entries = matrix.setdefault(row, {})
    for col, value in zip(cols, values, strict=True):
        row_entries[col] = value
    return True


def find_frequency_analysis(
    attempts: Sequence[Mapping[str, Any]],
    *,
    parse_analysis_fn: Callable[[Path], FrequencyAnalysis | None] | None = None,
) -> tuple[FrequencyAnalysis | None, int | None]:
    """Latest attempt output containing a frequency block, searched backwards.

    Returns the parsed analysis and the matching attempt's 1-based ``index``
    field (list position + 1 when absent).
    """
    for position in range(len(attempts) - 1, -1, -1):
        out_raw = str(attempts[position].get("out_path") or "").strip()
        if not out_raw:
            continue
        out_path = Path(out_raw)
        if not out_path.exists():
            continue
        try:
            analysis = (parse_analysis_fn or parse_frequency_analysis)(out_path)
        except OSError:
            continue
        if analysis is not None:
            index = int(attempts[position].get("index", position + 1) or (position + 1))
            return analysis, index
    return None, None


def mode_summaries(
    analysis: FrequencyAnalysis,
    alignment_pair: tuple[int, int] | None,
) -> tuple[ModeSummary, ...]:
    """All imaginary modes, or the lowest real mode when none are imaginary."""
    imaginary = [
        idx for idx, freq in enumerate(analysis.frequencies) if is_imaginary_frequency(freq)
    ]
    if imaginary:
        chosen = imaginary
    else:
        lowest_real = next(
            (idx for idx, freq in enumerate(analysis.frequencies) if freq > FREQ_EPS_CM),
            None,
        )
        chosen = [] if lowest_real is None else [lowest_real]

    summaries = []
    for mode_index in chosen:
        vector = analysis.mode_vector(mode_index)
        if not any(vector):
            continue
        summaries.append(
            ModeSummary(
                mode_index=mode_index,
                frequency_cm=analysis.frequencies[mode_index],
                imaginary=is_imaginary_frequency(analysis.frequencies[mode_index]),
                top_atoms=_top_atom_displacements(analysis, vector),
                scan_alignment=_pair_alignment(analysis, vector, alignment_pair),
            )
        )
    return tuple(summaries)


def _atom_displacement_vectors(vector: Sequence[float]) -> list[tuple[float, float, float]]:
    return [
        (vector[3 * atom], vector[3 * atom + 1], vector[3 * atom + 2])
        for atom in range(len(vector) // 3)
    ]


def _top_atom_displacements(
    analysis: FrequencyAnalysis,
    vector: Sequence[float],
) -> tuple[ModeAtomDisplacement, ...]:
    displacements = [
        ModeAtomDisplacement(
            atom_index=atom,
            element=analysis.atoms[atom][0] if atom < len(analysis.atoms) else "?",
            displacement=math.hypot(*components),
        )
        for atom, components in enumerate(_atom_displacement_vectors(vector))
    ]
    displacements.sort(key=lambda entry: -entry.displacement)
    return tuple(displacements[:_TOP_ATOM_COUNT])


def _pair_alignment(
    analysis: FrequencyAnalysis,
    vector: Sequence[float],
    alignment_pair: tuple[int, int] | None,
) -> float | None:
    """|relative motion of the pair along their bond| / sqrt(2), in [0, 1].

    1.0 means the (normalized) mode is exactly the two atoms moving against
    each other along their bond; ~0 means the mode ignores that coordinate.
    """
    if alignment_pair is None:
        return None
    i, j = alignment_pair
    if max(i, j) >= len(analysis.atoms) or 3 * max(i, j) + 2 >= len(vector):
        return None
    ri = analysis.atoms[i][1:]
    rj = analysis.atoms[j][1:]
    bond = [a - b for a, b in zip(ri, rj, strict=True)]
    bond_norm = math.hypot(*bond)
    if bond_norm <= 0:
        return None
    displacements = _atom_displacement_vectors(vector)
    relative = [a - b for a, b in zip(displacements[i], displacements[j], strict=True)]
    projection = abs(sum(r * b / bond_norm for r, b in zip(relative, bond, strict=True)))
    return min(1.0, projection / math.sqrt(2.0))
