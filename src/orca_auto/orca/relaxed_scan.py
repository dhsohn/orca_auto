from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .input_blocks import active_orca_line_text, find_block
from .parser import KCAL_PER_HARTREE
from .parser.io import open_orca_text

_FLOAT_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")
_SIMPLE_SCAN_COORD_LINE_RE = re.compile(
    rf"^(?P<prefix>\s*\S+(?:\s+\d+)+\s*=\s*)"
    rf"(?P<start>{_FLOAT_RE.pattern})(?P<sep1>\s*,\s*)"
    rf"(?P<end>{_FLOAT_RE.pattern})(?P<sep2>\s*,\s*)"
    r"(?P<points>\d+)(?P<suffix>.*)$"
)


@dataclass(frozen=True)
class ScanSurfacePoint:
    index: int
    coordinates: tuple[float, ...]
    energy: float


@dataclass(frozen=True)
class ScanCoordinateSpec:
    kind: str
    atoms: tuple[int, ...]
    start: float
    end: float
    points: int

    def label(self) -> str:
        atom_text = ",".join(str(atom) for atom in self.atoms)
        return f"{self.kind}({atom_text})"


# Lines that end the relaxed-surface tables when the SCF-energy table is
# absent (a truncated or older-format output). Without them, any later line
# holding two numbers (timings, the total run time) would be read as a point.
_SURFACE_SECTION_END_MARKERS = (
    "THE CALCULATED SURFACE USING THE SCF ENERGY",
    "TIMINGS",
    "TOTAL RUN TIME",
    "FINAL SINGLE POINT ENERGY",
    "OPTIMIZATION RUN DONE",
    "ORCA TERMINATED NORMALLY",
)

# A number ORCA could not print: the Fortran field overflowed to asterisks, or
# the value itself was not finite. `_FLOAT_RE` finds nothing in these, so a row
# holding one looks like prose unless it is recognised here.
_SPOILED_NUMBER_RE = re.compile(r"[-+]?(?:\*{2,}|nan|inf(?:inity)?)", re.IGNORECASE)
# A row is read piece by piece rather than by splitting on whitespace, because
# an overflowed Fortran field fills its own width with asterisks and so can
# abut the column beside it. Every actual-energy row in this lab's 36 real ORCA
# outputs is 28 columns — a 10-column coordinate ending at column 13, one
# blank, a 14-column energy — and rows that printed cannot say whether that
# blank is a literal separator or the energy field's own padding, so an
# overflowed energy can leave `1.86000000***************`: a single token.
_SURFACE_ROW_PIECE_RE = re.compile(
    rf"(?P<blank>\s+)|(?P<number>{_FLOAT_RE.pattern})|(?P<spoiled>{_SPOILED_NUMBER_RE.pattern})",
    re.IGNORECASE,
)


def _is_spoiled_surface_row(line: str) -> bool:
    """True for a surface row whose columns ORCA failed to print in full.

    The row happened and burned a step number even though no point can be read
    from it, so it must be counted. The test is deliberately narrow: the whole
    line has to be blanks, whole numbers and spoiled numbers, with at least one
    number and at least one spoiled number present. Requiring a readable number
    keeps an asterisk rule line (``***** *****``) prose — asterisk banners run
    to 387,179 lines across this lab's 792 ORCA outputs, while no row losing
    every column at once appears in any of them.
    """
    position = 0
    readable = 0
    spoiled = 0
    for piece in _SURFACE_ROW_PIECE_RE.finditer(line):
        if piece.start() != position:
            # A gap means the line holds something that is neither a blank nor
            # a number in any state: prose, a dashed rule, a label.
            return False
        position = piece.end()
        if piece.group("number") is not None:
            readable += 1
        elif piece.group("spoiled") is not None:
            spoiled += 1
    return position == len(line) and readable > 0 and spoiled > 0


def parse_scan_actual_surface(out_path: Path) -> list[ScanSurfacePoint]:
    """Points of the relaxed-scan table computed with the actual energy.

    A row is one or more scan coordinates followed by a total energy in Eh,
    which is negative and finite for every real molecule. Every row must be as
    wide as the table, and rows of any other width are refused; the table ends
    at the SCF energy table or at the first later section marker. Anything else
    is a non-row and is refused rather than read as a point.

    The table's width is that of the first line holding two numbers, unless no
    valid row is that wide — then it is the width most of the valid rows share,
    so a malformed leading row no longer refuses the whole table.
    """
    candidates: list[ScanSurfacePoint] = []
    # Insertion-ordered, so a tie in the fallback below resolves to the width
    # that appeared first rather than to an arbitrary one.
    valid_widths: dict[int, int] = {}
    in_actual_surface = False
    first_row_width: int | None = None
    # ORCA numbers the scan steps by table row (`<base>.NNN.xyz`); a refused
    # or unprintable row keeps its number so the points after it still address
    # the right step geometry.
    row_number = 0
    try:
        # Decoded by the parser's rule so the table matches the verdict's text.
        with open_orca_text(out_path) as handle:
            for line in handle:
                upper = line.upper()
                if "THE CALCULATED SURFACE USING THE 'ACTUAL ENERGY'" in upper:
                    in_actual_surface = True
                    continue
                if not in_actual_surface:
                    continue
                if any(marker in upper for marker in _SURFACE_SECTION_END_MARKERS):
                    break
                values = [float(match.group(0)) for match in _FLOAT_RE.finditer(line)]
                if len(values) < 2:
                    if _is_spoiled_surface_row(line):
                        row_number += 1
                    continue
                row_number += 1
                if first_row_width is None:
                    first_row_width = len(values)
                energy = values[-1]
                if (
                    not math.isfinite(energy)
                    or energy >= 0.0
                    or not all(math.isfinite(value) for value in values[:-1])
                ):
                    continue
                valid_widths[len(values)] = valid_widths.get(len(values), 0) + 1
                candidates.append(
                    ScanSurfacePoint(
                        index=row_number,
                        coordinates=tuple(values[:-1]),
                        energy=energy,
                    )
                )
    except OSError:
        return []
    if not candidates:
        return []
    if first_row_width is not None and first_row_width in valid_widths:
        # The first row's width still wins whenever any valid row shares it, so
        # this rule only ever adds rows the old one refused.
        row_width = first_row_width
    else:
        # No valid row is as wide as the first one, which is how a malformed
        # leading row used to refuse the whole table. Take the width the
        # surviving rows agree on instead.
        row_width = max(valid_widths, key=lambda width: valid_widths[width])
    return [point for point in candidates if len(point.coordinates) + 1 == row_width]


def scan_profile_interior_barrier_kcal(energies: Sequence[float]) -> float | None:
    """Prominence in kcal/mol of the highest interior maximum along a scan profile.

    Prominence is the smaller of the climbs from the lowest energy on either
    side of a point, so a profile that is monotonic up to noise scores ~0 while
    a genuine barrier scores its height above the shallower flank. ``None``
    when fewer than three points exist (no interior point to judge).
    """
    if len(energies) < 3:
        return None
    suffix_min = list(energies)
    for idx in range(len(suffix_min) - 2, -1, -1):
        suffix_min[idx] = min(suffix_min[idx], suffix_min[idx + 1])
    best = 0.0
    prefix_min = energies[0]
    for idx in range(1, len(energies) - 1):
        prominence = min(energies[idx] - prefix_min, energies[idx] - suffix_min[idx + 1])
        best = max(best, prominence)
        prefix_min = min(prefix_min, energies[idx])
    return best * KCAL_PER_HARTREE


def parse_scan_coordinate(text: str) -> ScanCoordinateSpec | None:
    """Spec from a bare scan-coordinate string like ``B 0 1 = 1.20, 3.00, 10``."""
    match = _SIMPLE_SCAN_COORD_LINE_RE.match(text.strip())
    if match is None:
        return None
    prefix_tokens = match.group("prefix").split("=")[0].split()
    if len(prefix_tokens) < 2:
        return None
    try:
        atoms = tuple(int(token) for token in prefix_tokens[1:])
    except ValueError:
        return None
    return ScanCoordinateSpec(
        kind=prefix_tokens[0].upper(),
        atoms=atoms,
        start=float(match.group("start")),
        end=float(match.group("end")),
        points=int(match.group("points")),
    )


def first_scan_coordinate_spec(inp_path: Path) -> ScanCoordinateSpec | None:
    """Kind, atom indices, and range of the first simple scan coordinate line."""
    try:
        lines = inp_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    for idx in _simple_scan_coord_line_indices(lines):
        spec = parse_scan_coordinate(active_orca_line_text(lines[idx]))
        if spec is not None:
            return spec
    return None


def _simple_scan_coord_line_indices(lines: list[str]) -> list[int]:
    """Line indices of simple scan coordinates inside ``%geom`` ``scan ... end`` sub-blocks.

    Uses the package's shared block rule: :func:`find_block` already keeps the
    nested ``scan`` rows and their closing ``end`` row as body rows of the
    ``%geom`` block, so no separate end-of-sub-block rule is needed here.
    """
    block = find_block(lines, "geom")
    if block is None:
        return []
    indices: list[int] = []
    in_scan = False
    for row in block.rows:
        if row.line_index == block.start:
            continue
        single = row.tokens[0].value.lower() if len(row.tokens) == 1 else ""
        if not in_scan:
            in_scan = single == "scan"
            continue
        if single == "end":
            in_scan = False
            continue
        if _SIMPLE_SCAN_COORD_LINE_RE.match(active_orca_line_text(lines[row.line_index])):
            indices.append(row.line_index)
    return indices
