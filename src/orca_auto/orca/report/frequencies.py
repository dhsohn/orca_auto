"""Frequency, normal-mode, and geometry parsing plus vibrational summaries.

The parser streams an ORCA output once and keeps the LAST ``VIBRATIONAL
FREQUENCIES`` / ``NORMAL MODES`` / ``CARTESIAN COORDINATES`` blocks, so
multi-job outputs report the final frequency calculation. Summaries condense a
mode into its dominant atom displacements and, when a bond pair is given, its
alignment with that coordinate.
"""

from __future__ import annotations

import html
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FREQ_EPS_CM = 1.0
_TOP_ATOM_COUNT = 5

_FREQ_HEADER = "VIBRATIONAL FREQUENCIES"
_MODES_HEADER = "NORMAL MODES"
_COORDS_HEADER = "CARTESIAN COORDINATES (ANGSTROEM)"
_FREQ_LINE_RE = re.compile(r"^\s*(\d+):\s*(-?\d+(?:\.\d+)?)\s*cm\*\*-1", re.IGNORECASE)
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
        return sum(1 for freq in self.frequencies if freq < -FREQ_EPS_CM)


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
    freqs: list[float] | None = None
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

    try:
        with out_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                stripped = line.strip()
                upper = stripped.upper()
                if upper == _FREQ_HEADER:
                    close_section()
                    section, current_freqs = "freq", []
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
                    match = _FREQ_LINE_RE.match(line)
                    if match is not None:
                        current_freqs.append(float(match.group(2)))
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
    except OSError:
        return None

    if freqs is None:
        return None
    return FrequencyAnalysis(
        frequencies=tuple(freqs),
        mode_matrix=modes or {},
        atoms=tuple(coords or []),
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
        analysis = parse_frequency_analysis(out_path)
        if analysis is not None:
            index = int(attempts[position].get("index", position + 1) or (position + 1))
            return analysis, index
    return None, None


def mode_summaries(
    analysis: FrequencyAnalysis,
    alignment_pair: tuple[int, int] | None,
) -> tuple[ModeSummary, ...]:
    """All imaginary modes, or the lowest real mode when none are imaginary."""
    imaginary = [idx for idx, freq in enumerate(analysis.frequencies) if freq < -FREQ_EPS_CM]
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
                imaginary=analysis.frequencies[mode_index] < -FREQ_EPS_CM,
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


def mode_section_html(
    summaries: tuple[ModeSummary, ...],
    alignment_label: str | None,
) -> str:
    if not summaries:
        return (
            '<p class="muted">No frequency calculation found in any attempt output, '
            "so no vibrational summary is available.</p>"
        )
    blocks = []
    for summary in summaries:
        kind = "imaginary mode" if summary.imaginary else "lowest real mode"
        freq_text = f"{summary.frequency_cm:.1f} cm&#8315;&#185;"
        atoms = ", ".join(
            f"{entry.element}{entry.atom_index} ({entry.displacement:.2f})"
            for entry in summary.top_atoms
        )
        alignment_html = ""
        if alignment_label is not None and summary.scan_alignment is not None:
            alignment_html = (
                f'<div class="sub">Alignment with {html.escape(alignment_label)}: '
                f"{summary.scan_alignment * 100:.0f}%</div>"
            )
        blocks.append(
            '<div class="mode">'
            f"<div><strong>Mode {summary.mode_index}</strong> &#183; {kind} &#183; "
            f"&#957; = {freq_text}</div>"
            f'<div class="sub">Top atom displacements (weighted norm): {html.escape(atoms)}'
            "</div>"
            f"{alignment_html}"
            "</div>"
        )
    return "".join(blocks)
