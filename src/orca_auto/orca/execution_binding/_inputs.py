"""Route, XYZ and inline-geometry inspection of ORCA input text."""

from __future__ import annotations

import io
import math
import re
from pathlib import Path

from orca_auto.orca.geometry_limits import MAX_ADMISSION_ATOMS

from ..input_blocks import GRADIENT_RUN_TYPES, ParsedInput, find_geometry_block
from ..operation_facts import OperationFacts
from ._models import _RouteOutputs

_XYZ_ATOM_LABEL_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_:+().{}\[\]-]*\Z")


_XYZ_COORDINATE_RE = re.compile(r"\A[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?\Z")


def _inline_geometry_atom_count(selected: ParsedInput) -> int | None:
    """Atom rows of the inline ``* xyz`` block; ``None`` for ``xyzfile`` or no geometry.

    Rows come from the shared comment-aware geometry scanner, so a comment-only
    line inside the block is not an atom and cannot trip the admission limits.
    """

    block = selected.geometry
    if block is None or block.kind != "xyz":
        return None
    atom_count = len(block.atom_rows)
    if atom_count > MAX_ADMISSION_ATOMS:
        raise ValueError(
            f"ORCA molecule exceeds the server atom-count limit of {MAX_ADMISSION_ATOMS}"
        )
    return atom_count


def _runtime_outputs(facts: OperationFacts) -> _RouteOutputs:
    """The runtime outputs one input's operation writes; build and verify both read it here."""

    route = facts.route
    return _RouteOutputs(
        engrad_is_output=facts.route_writes_engrad
        or any(
            not run_type.quoted
            and run_type.value is not None
            and run_type.value.lower() in GRADIENT_RUN_TYPES
            for run_type in facts.method_run_types
        ),
        hessian_requested=route.requests_frequency,
        neb_requested=facts.neb_route,
        neb_preopt_ends=facts.neb_preopt_ends,
        same_stem_xyz_is_output=route.is_opt
        or route.is_ts
        or route.is_irc
        or facts.neb_route
        or facts.goat_route,
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
