"""Route, XYZ and inline-geometry inspection of ORCA input text."""

from __future__ import annotations

import io
import math
import re
from pathlib import Path

from orca_auto.core.geometry_limits import MAX_ADMISSION_ATOMS

from ..completion_rules import IRC_ROUTE_RE, OPT_ROUTE_RE, TS_ROUTE_RE
from ..input_blocks import find_geometry_block, orca_route_line, orca_route_tokens
from ..job_type import FREQ_RE

_NEB_ROUTE_RE = re.compile(r"\b(?:ZOOM-)?NEB(?:-(?:TS|CI))?\b", re.IGNORECASE)


_ENGRAD_EXACT_ROUTE_KEYWORDS = frozenset(
    {
        "ci-opt",
        "crudeopt",
        "engrad",
        "energygrad",
        "l-opt",
        "l-opth",
        "numgrad",
        "opth",
        "optts",
        "qmmmopt",
        "sloppyopt",
    }
)


_XYZ_ATOM_LABEL_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_:+().{}\[\]-]*\Z")


_XYZ_COORDINATE_RE = re.compile(r"\A[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?\Z")


def _inline_geometry_atom_count(selected_text: str) -> int | None:
    """Atom rows of the inline ``* xyz`` block; ``None`` for ``xyzfile`` or no geometry.

    Rows come from the shared comment-aware geometry scanner, so a comment-only
    line inside the block is not an atom and cannot trip the admission limits.
    """

    block = find_geometry_block(selected_text.splitlines())
    if block is None or block.kind != "xyz":
        return None
    atom_count = len(block.atom_rows)
    if atom_count > MAX_ADMISSION_ATOMS:
        raise ValueError(
            f"ORCA molecule exceeds the server atom-count limit of {MAX_ADMISSION_ATOMS}"
        )
    return atom_count


def _route_requests_hessian(lines: list[str]) -> bool:
    route_text = " ".join(route for line in lines if (route := orca_route_line(line)) is not None)
    return bool(FREQ_RE.search(route_text))


def _route_writes_engrad(lines: list[str]) -> bool:
    for line in lines:
        for token in orca_route_tokens(line):
            if token.quoted:
                continue
            # ORCA's keywords ending in "Opt" do not uniformly emit this
            # file, so keep the nonstandard optimization spellings exact.
            if token.value.casefold() in _ENGRAD_EXACT_ROUTE_KEYWORDS or any(
                pattern.fullmatch(token.value) is not None
                for pattern in (OPT_ROUTE_RE, IRC_ROUTE_RE)
            ):
                return True
    return False


def _route_writes_same_stem_xyz(lines: list[str]) -> bool:
    route_text = " ".join(route for line in lines if (route := orca_route_line(line)) is not None)
    return any(
        pattern.search(route_text) is not None
        for pattern in (OPT_ROUTE_RE, TS_ROUTE_RE, IRC_ROUTE_RE, _NEB_ROUTE_RE)
    )


def _validated_xyz_atom_count(path: Path, payload: bytes, *, max_atoms: int) -> int:
    try:
        lines = io.StringIO(payload.decode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ValueError(f"ORCA XYZ geometry must be UTF-8 text: {path}") from exc
    header = ""
    for line in lines:
        header = line.strip()
        if header:
            break
    try:
        atom_count = int(header)
    except ValueError as exc:
        raise ValueError(f"ORCA XYZ geometry has an invalid atom count: {path}") from exc
    if atom_count <= 0:
        raise ValueError(f"ORCA XYZ geometry must contain at least one atom: {path}")
    if atom_count > max_atoms:
        raise ValueError(f"ORCA molecule exceeds the server atom-count limit of {max_atoms}")
    return atom_count


def _strict_xyz_atom_row(path: Path, line: str) -> str:
    tokens = line.split()
    if len(tokens) != 4 or _XYZ_ATOM_LABEL_RE.fullmatch(tokens[0]) is None:
        raise ValueError(f"ORCA XYZ geometry has an invalid atom row: {path}")
    if any(_XYZ_COORDINATE_RE.fullmatch(value) is None for value in tokens[1:]):
        raise ValueError(f"ORCA XYZ geometry has invalid coordinates: {path}")
    coordinates = tuple(float(value.replace("d", "e").replace("D", "E")) for value in tokens[1:])
    if not all(math.isfinite(value) for value in coordinates):
        raise ValueError(f"ORCA XYZ geometry has non-finite coordinates: {path}")
    return tokens[0].casefold()


def _xyz_atom_lines(path: Path, payload: bytes, *, max_atoms: int) -> list[str]:
    # ``payload`` is a standard XYZ file (count / comment / atom rows), not ORCA
    # input syntax: ``#`` is not a comment marker there, so the ORCA line
    # tokenizer deliberately does not apply and every declared row stays strict.
    atom_count = _validated_xyz_atom_count(path, payload, max_atoms=max_atoms)
    try:
        lines = payload.decode("utf-8", errors="strict").splitlines()
    except UnicodeError as exc:
        raise ValueError(f"ORCA XYZ geometry must be UTF-8 text: {path}") from exc
    header_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        -1,
    )
    atom_start = header_index + 2
    atom_lines = lines[atom_start : atom_start + atom_count]
    if header_index < 0 or len(atom_lines) != atom_count:
        raise ValueError(f"ORCA XYZ geometry has fewer atoms than declared: {path}")
    for line in atom_lines:
        _strict_xyz_atom_row(path, line)
    if any(line.strip() for line in lines[atom_start + atom_count :]):
        raise ValueError(f"ORCA XYZ geometry has trailing rows after its declared atoms: {path}")
    return atom_lines


def _inline_geometry_atom_signature(path: Path, payload: bytes) -> tuple[str, ...]:
    try:
        lines = payload.decode("utf-8", errors="strict").splitlines()
    except UnicodeError as exc:
        raise ValueError(f"ORCA inline geometry must be UTF-8 text: {path}") from exc
    block = find_geometry_block(lines)
    if block is not None and block.kind != "xyz":
        raise ValueError(f"ORCA recovery bound input has no inline geometry: {path}")
    if block is None or block.terminator_index is None:
        raise ValueError(f"ORCA recovery bound input has no complete inline geometry: {path}")
    if not block.atom_rows:
        raise ValueError(f"ORCA recovery bound input has an empty geometry: {path}")
    return tuple(_strict_xyz_atom_row(path, text) for _index, text in block.atom_rows)
