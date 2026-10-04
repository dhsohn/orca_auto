"""Frequency, normal-mode, and geometry parsing plus vibrational summaries.

The parser streams an ORCA output once and keeps the LAST ``VIBRATIONAL
FREQUENCIES`` / ``NORMAL MODES`` / ``CARTESIAN COORDINATES`` blocks. A
frequency or mode block that is followed by another ``FINAL SINGLE POINT
ENERGY`` line belongs to an earlier geometry (the initial or a recalculated
Hessian of an optimization) and is discarded, so only a frequency calculation
at the final geometry is reported. The last frequency header decides: if it
printed no supported value, or any line of it prints ``cm**-1`` without one
(``NaN cm**-1``), no frequencies are reported rather than an earlier section or
a truncated one. Summaries condense a mode into its dominant
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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .output_status import is_execution_output_line, iter_output_lines

logger = logging.getLogger(__name__)

# Negative modes at or below this magnitude are numerical noise, not a reaction
# coordinate. The completion analyzer and the SI/report renderers all count
# through is_imaginary_frequency, so a verified TS can never be re-counted
# differently in the published SI.
IMAGINARY_FREQ_THRESHOLD_CM1 = 10.0
_TOP_ATOM_COUNT = 5

_FREQ_HEADER = "VIBRATIONAL FREQUENCIES"
# A line that starts with this phrase after its leading whitespace, in any case
# and whatever follows, ends the frequency sections before it. That is broader
# than the parser's energy rule on purpose: a final energy line the parser
# cannot read (an overflowed value, trailing text) still means a later
# geometry, so a stale Hessian never verifies it.
_FINAL_ENERGY_HEADER = "FINAL SINGLE POINT ENERGY"
_MODES_HEADER = "NORMAL MODES"
_COORDS_HEADER = "CARTESIAN COORDINATES (ANGSTROEM)"
# One printed wavenumber: a signed number followed by ``cm**-1``, preceded by
# the line start, whitespace, or the mode index's colon (``   6:  -412.34 cm**-1``).
# This is the rule the completion analyzer has always verified a TS by; it
# also accepts the numbered form ORCA prints.
FREQUENCY_VALUE_RE = re.compile(r"(?:^|[\s:])(-?\d+(?:\.\d+)?)\s*cm\*\*-1", re.IGNORECASE)
# Compared in lower case: a line in a frequency section that prints this unit
# more often than it holds supported values (``NaN cm**-1``, ``Inf cm**-1``)
# invalidates the whole section instead of truncating it.
_FREQUENCY_UNIT = "cm**-1"
# Unlike the parser's coordinate row: any symbol (DA, lower case), decimal xyz, nothing after z.
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
    section was superseded by a later final energy, or the last one printed
    no frequencies or an unsupported value, which is not the same as an
    output that never ran a frequency calculation.
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
    current_invalid = False
    current_modes: dict[int, dict[int, float]] = {}
    current_cols: list[int] = []
    current_coords: list[tuple[str, float, float, float]] = []

    def close_section() -> None:
        nonlocal freqs, modes, coords, section, started
        if section == "freq":
            # The last header decides: one that printed no frequency, or an
            # unsupported value, clears an earlier section instead of letting
            # it stand for this Hessian.
            freqs = list(current_freqs) if current_freqs and not current_invalid else None
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
            section, current_freqs, current_invalid = "freq", [], False
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
            if line.lower().count(_FREQUENCY_UNIT) > len(values):
                # The rest of the section is ignored until the next header.
                current_invalid = True
                close_section()
            elif values:
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
            (
                idx
                for idx, freq in enumerate(analysis.frequencies)
                if freq > IMAGINARY_FREQ_THRESHOLD_CM1
            ),
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
