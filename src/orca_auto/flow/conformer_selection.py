"""The shared conformer-selection contract for ΔE_int fan-out and SI assembly.

The interaction-energy materializer (``flow/orchestration``) decides which
optimized complexes fan out single points, and the workflow SI
(``flow/workflow``) later reassembles ΔE_int per representative parent. Both
must apply the *same* minimum-eligibility, selected-input consistency,
geometry-match, single-point pairing, and RMSD-representative rules: because
both ends fail closed, any disagreement does not corrupt numbers — it silently
degrades an enabled feature into blocker notes. This module is the single
source of those rules; neither consumer may reimplement them locally.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

from orca_auto.core.engine_process import read_confined_text, require_confined_regular_file
from orca_auto.core.engine_runner import executable_identity
from orca_auto.core.geometry_limits import MAX_ADMISSION_ATOMS
from orca_auto.core.queue.engine.input_snapshot import MAX_INPUT_SNAPSHOT_BYTES
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.orca.input_blocks import (
    BLOCK_START_RE,
    GEOM_HEADER_RE,
    MAXCORE_DIRECTIVE_RE,
    PAL_ROUTE_TOKEN_RE,
    OrcaFileReference,
    active_orca_directive_text,
    active_orca_line_text,
    file_route_lines,
    geometry_range,
    orca_line_tokens,
    orca_route_line,
    orca_route_tokens,
    scan_orca_file_references,
)
from orca_auto.orca.report.rmsd import (
    RmsdCandidate,
    RmsdGrouping,
    group_by_rmsd,
    rmsd_comparison_key,
)
from orca_auto.orca.report.si import SiBlock

from .manifest import DEFAULT_RMSD_ENERGY_WINDOW_KCAL, DEFAULT_RMSD_THRESHOLD_ANGSTROM

# Two geometries printed by ORCA to 6 decimals are "the same structure" well
# below this; an SP run on anything else (reordered atoms, re-optimized
# geometry) must not pair. The epsilon only absorbs binary representation noise
# at the tolerance boundary and is far below coordinate print precision.
GEOMETRY_TOL_ANGSTROM = 1e-4
GEOMETRY_COMPARISON_EPSILON_ANGSTROM = 1e-12

_COORDINATE_TOLERANCE = GEOMETRY_TOL_ANGSTROM + GEOMETRY_COMPARISON_EPSILON_ANGSTROM


@dataclass(frozen=True)
class OrcaSelectedInputScienceIdentity:
    """Science-affecting identity of one bound, generation-local ORCA input."""

    route_tokens: tuple[tuple[bool, str], ...]
    charge: int
    multiplicity: int
    directive_sha256: str
    atom_sequence: tuple[str, ...]
    dependency_identities: tuple[tuple[str, str, int], ...]


def finite(value: float | None) -> TypeGuard[float]:
    return value is not None and math.isfinite(value)


def normalized_route_line(value: Any) -> str:
    """Canonical route text: every ``!`` dropped, whitespace collapsed, lowercased.

    ``OrcaResult.input_line`` merges all route lines of the echoed input
    *without* their ``!`` markers, while ``file_route_lines`` keeps a leading
    ``"! "`` per route line. Dropping every ``!`` before comparing is the only
    normalization under which a multi-route-line input equals its own echo.
    """
    text = normalize_text(value).replace("!", " ")
    return " ".join(text.split()).lower()


def orca_science_route_identity(lines: list[str]) -> tuple[tuple[bool, str], ...] | None:
    """Canonical active route tokens with resource-only ``PAL<n>`` omitted."""

    route_seen = False
    canonical: list[tuple[bool, str]] = []
    for line in lines:
        if orca_route_line(line) is None:
            continue
        route_seen = True
        canonical.extend(
            (
                token.quoted,
                token.value if token.quoted else token.value.casefold(),
            )
            for token in orca_route_tokens(line)
            if token.quoted or PAL_ROUTE_TOKEN_RE.fullmatch(token.value) is None
        )
    return tuple(canonical) if route_seen and canonical else None


def bound_orca_selected_input_science_identity(
    generation_dir: Path,
    selected_path: Path,
    *,
    bound_selected_identity: Mapping[str, Any],
    materialized_input_identities: Mapping[str, Any],
) -> OrcaSelectedInputScienceIdentity | None:
    """Read one provenance-bound selected input into a comparison identity.

    Route keywords, charge/multiplicity, active non-resource directives, and
    ordered atom labels affect cross-stage energy comparability. CPU/memory
    controls do not. The selected input is confined to the generation and its
    executable identity is checked both before and after dependent reads.
    """

    try:
        resolved_generation = generation_dir.expanduser().resolve(strict=True)
        raw_selected = selected_path.expanduser()
        selected = require_confined_regular_file(
            resolved_generation,
            raw_selected,
            label="ORCA selected input science identity",
        )
        expected_identity = dict(bound_selected_identity)
        if (
            not raw_selected.is_absolute()
            or raw_selected != selected
            or selected.parent != resolved_generation
            or executable_identity(selected) != expected_identity
        ):
            return None
        lines = read_confined_text(
            resolved_generation,
            selected,
            label="ORCA selected input science identity",
            max_bytes=MAX_INPUT_SNAPSHOT_BYTES,
        ).splitlines()
        references = scan_orca_file_references(lines)
        verified_references = _verified_materialized_references(
            resolved_generation,
            selected,
            references,
            materialized_input_identities,
        )
        if verified_references is None:
            return None
        geometries = [
            match for line in lines if (match := GEOM_HEADER_RE.match(line.strip())) is not None
        ]
        geometry = geometry_range(lines)
        route_tokens = orca_science_route_identity(lines)
        directive_sha256 = _orca_science_directive_fingerprint(lines, references)
        atom_sequence = _orca_input_atom_sequence(
            generation_dir=resolved_generation,
            lines=lines,
            verified_references=verified_references,
        )
        if (
            len(geometries) != 1
            or geometry is None
            or route_tokens is None
            or directive_sha256 is None
            or atom_sequence is None
            or executable_identity(selected) != expected_identity
            or any(
                executable_identity(path) != expected
                for _reference, path, expected in verified_references
            )
        ):
            return None
        dependency_identities = tuple(
            (
                reference.kind,
                str(expected.get("sha256") or ""),
                int(expected.get("size_bytes", -1)),
            )
            for reference, _path, expected in verified_references
            if reference.kind != "geometry"
        )
        if any(not sha256 or size_bytes < 0 for _kind, sha256, size_bytes in dependency_identities):
            return None
        return OrcaSelectedInputScienceIdentity(
            route_tokens=route_tokens,
            charge=geometry[2],
            multiplicity=geometry[3],
            directive_sha256=directive_sha256,
            atom_sequence=atom_sequence,
            dependency_identities=dependency_identities,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _verified_materialized_references(
    generation_dir: Path,
    selected_path: Path,
    references: list[OrcaFileReference],
    materialized_input_identities: Mapping[str, Any],
) -> list[tuple[OrcaFileReference, Path, dict[str, Any]]] | None:
    """Resolve every semantic file reference to exactly one bound dependency."""

    verified: list[tuple[OrcaFileReference, Path, dict[str, Any]]] = []
    for reference in references:
        raw_reference = Path(reference.value).expanduser()
        if not raw_reference.is_absolute():
            raw_reference = selected_path.parent / raw_reference
        try:
            resolved_reference = require_confined_regular_file(
                generation_dir,
                raw_reference,
                label="ORCA selected input materialized dependency",
            )
        except (OSError, RuntimeError, ValueError):
            return None
        matches: list[dict[str, Any]] = []
        for raw_identity in materialized_input_identities.values():
            if not isinstance(raw_identity, Mapping):
                continue
            identity = dict(raw_identity)
            identity_path = Path(str(identity.get("path") or "")).expanduser()
            try:
                resolved_identity_path = require_confined_regular_file(
                    generation_dir,
                    identity_path,
                    label="ORCA materialized dependency identity",
                )
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved_identity_path == resolved_reference:
                matches.append(identity)
        if len(matches) != 1 or executable_identity(resolved_reference) != matches[0]:
            return None
        verified.append((reference, resolved_reference, matches[0]))
    return verified


def _orca_science_directive_fingerprint(
    lines: list[str],
    references: list[OrcaFileReference],
) -> str | None:
    """Digest active non-route science controls, excluding geometry/resources."""

    geometry = geometry_range(lines)
    if geometry is None:
        return None
    geometry_start, geometry_end, _charge, _multiplicity = geometry
    canonical_lines = list(lines)
    references_by_line: dict[int, list[OrcaFileReference]] = {}
    for reference in references:
        references_by_line.setdefault(reference.line_index, []).append(reference)
    for line_index, line_references in references_by_line.items():
        canonical_line = canonical_lines[line_index]
        for reference in sorted(line_references, key=lambda item: item.start, reverse=True):
            placeholder = f'"__orca_{reference.kind}_dependency__"'
            canonical_line = (
                canonical_line[: reference.start] + placeholder + canonical_line[reference.end :]
            )
        canonical_lines[line_index] = canonical_line
    canonical_tokens: list[tuple[bool, str]] = []
    in_pal_block = False
    for index, line in enumerate(canonical_lines):
        if geometry_start <= index < geometry_end:
            continue
        active_directive = active_orca_directive_text(line)
        active_text = active_orca_line_text(line).strip()
        if not active_text:
            continue
        tokens = orca_line_tokens(active_directive)
        if in_pal_block:
            if any(not token.quoted and token.value.casefold() == "end" for token in tokens):
                in_pal_block = False
            continue
        block = BLOCK_START_RE.match(active_directive)
        if block is not None and block.group(1).casefold() == "pal":
            if not any(not token.quoted and token.value.casefold() == "end" for token in tokens):
                in_pal_block = True
            continue
        if MAXCORE_DIRECTIVE_RE.match(active_directive) is not None:
            continue
        if orca_route_line(line) is not None:
            continue
        canonical_tokens.extend(
            (
                token.quoted,
                token.value if token.quoted else token.value.casefold(),
            )
            for token in orca_line_tokens(active_text)
        )
    if in_pal_block:
        return None
    canonical = json.dumps(canonical_tokens, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _orca_input_atom_sequence(
    *,
    generation_dir: Path,
    lines: list[str],
    verified_references: list[tuple[OrcaFileReference, Path, dict[str, Any]]],
) -> tuple[str, ...] | None:
    """Ordered atom labels from the bound inline or confined XYZ geometry."""

    geometry = geometry_range(lines)
    if geometry is None:
        return None
    start, end, _charge, _multiplicity = geometry
    header = GEOM_HEADER_RE.match(lines[start].strip())
    if header is None:
        return None
    if header.group(1).casefold() == "xyz":
        if end <= start + 1 or lines[end - 1].strip() != "*":
            return None
        return _orca_atom_sequence_from_rows(lines[start + 1 : end - 1], exact_columns=True)

    if len(orca_line_tokens(lines[start])) != 5:
        return None
    geometry_references = [
        (reference, path)
        for reference, path, _identity in verified_references
        if reference.kind == "geometry" and reference.line_index == start
    ]
    if len(geometry_references) != 1:
        return None
    _reference, geometry_path = geometry_references[0]
    try:
        xyz_lines = read_confined_text(
            generation_dir,
            geometry_path,
            label="ORCA selected XYZ science identity",
            max_bytes=MAX_INPUT_SNAPSHOT_BYTES,
        ).splitlines()
    except (OSError, RuntimeError, ValueError):
        return None
    cursor = 0
    while cursor < len(xyz_lines) and not xyz_lines[cursor].strip():
        cursor += 1
    if cursor >= len(xyz_lines):
        return None
    try:
        atom_count = int(xyz_lines[cursor].strip())
    except ValueError:
        return None
    if atom_count < 1 or atom_count > MAX_ADMISSION_ATOMS:
        return None
    row_start = cursor + 2
    row_end = row_start + atom_count
    if row_end > len(xyz_lines) or any(line.strip() for line in xyz_lines[row_end:]):
        return None
    return _orca_atom_sequence_from_rows(
        xyz_lines[row_start:row_end],
        exact_columns=False,
    )


def _orca_atom_sequence_from_rows(
    rows: list[str],
    *,
    exact_columns: bool,
) -> tuple[str, ...] | None:
    if not rows or len(rows) > MAX_ADMISSION_ATOMS:
        return None
    labels: list[str] = []
    for row in rows:
        tokens = row.split()
        if len(tokens) != 4 if exact_columns else len(tokens) < 4:
            return None
        try:
            coordinates = tuple(float(value) for value in tokens[1:4])
        except ValueError:
            return None
        if not tokens[0] or not all(math.isfinite(value) for value in coordinates):
            return None
        labels.append(tokens[0].casefold())
    return tuple(labels)


def coordinates_match(
    expected: Sequence[Sequence[Any]],
    actual: Sequence[Sequence[Any]],
) -> bool:
    """Same elements in the same order, positions within the shared tolerance."""
    if len(expected) != len(actual) or not expected:
        return False
    for expected_row, actual_row in zip(expected, actual, strict=True):
        if expected_row[0] != actual_row[0]:
            return False
        if any(
            abs(left - right) > _COORDINATE_TOLERANCE
            for left, right in zip(expected_row[1:], actual_row[1:], strict=True)
        ):
            return False
    return True


def blocks_match_geometry(a: SiBlock, b: SiBlock) -> bool:
    return coordinates_match(a.result.coordinates, b.result.coordinates)


def has_required_provenance(block: SiBlock) -> bool:
    result = block.result
    return bool(
        result.input_line.strip()
        and result.orca_version.strip()
        and result.electronic_state_verified
    )


def selected_input_state_matches(block: SiBlock, state: Mapping[str, Any]) -> bool:
    """The executed charge/multiplicity/route must match the selected input file.

    Fails closed: an unreadable selected input, a missing geometry header, or
    an empty route never matches.
    """
    selected_raw = normalize_text(state.get("selected_inp"))
    if not selected_raw:
        return False
    selected_path = Path(selected_raw)
    try:
        lines = selected_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False
    geometry = geometry_range(lines)
    if geometry is None:
        return False
    selected_route = normalized_route_line(" ".join(file_route_lines(selected_path)))
    return (
        (block.result.charge, block.result.multiplicity) == (geometry[2], geometry[3])
        and bool(selected_route)
        and selected_route == normalized_route_line(block.result.input_line)
    )


def eligible_minimum_block(
    block: SiBlock,
    *,
    expected_charge: int | None = None,
    expected_multiplicity: int | None = None,
) -> bool:
    """Fail-closed "usable optimized minimum" shared by fan-out and SI assembly.

    A block qualifies only as a converged, vibrationally clean minimum with a
    finite energy, finite non-empty coordinates, and full verified provenance;
    optionally pinned to an expected electronic state.
    """
    result = block.result
    return bool(
        block.kind == "min"
        and result.opt_converged is True
        and block.imaginary_count in (None, 0)
        and finite(result.energy_hartree)
        and bool(result.coordinates)
        and all(math.isfinite(value) for _element, *xyz in result.coordinates for value in xyz)
        and has_required_provenance(block)
        and (expected_charge is None or result.charge == expected_charge)
        and (expected_multiplicity is None or result.multiplicity == expected_multiplicity)
    )


def unique_single_point_matches(
    stationary_blocks: Sequence[SiBlock],
    single_point_blocks: Sequence[SiBlock],
) -> list[int | None]:
    """Globally unique 1:1 geometry/electronic-state single-point matches.

    Returns, aligned with ``stationary_blocks``, the index of the single point
    that uniquely refines that structure, or ``None``. Both 1:N (multiple SPs
    for one structure) and N:1 (one SP matching several structures) ambiguity
    yield ``None`` — never a pick by order.
    """
    matches_by_stationary: list[list[int]] = []
    match_counts = [0] * len(single_point_blocks)
    for block in stationary_blocks:
        matches = [
            index
            for index, single_point in enumerate(single_point_blocks)
            if has_required_provenance(block)
            and has_required_provenance(single_point)
            and finite(single_point.result.energy_hartree)
            and block.result.charge == single_point.result.charge
            and block.result.multiplicity == single_point.result.multiplicity
            and blocks_match_geometry(block, single_point)
        ]
        matches_by_stationary.append(matches)
        for index in matches:
            match_counts[index] += 1
    return [
        matches[0] if len(matches) == 1 and match_counts[matches[0]] == 1 else None
        for matches in matches_by_stationary
    ]


def rmsd_candidate_for_block(
    stage_id: str,
    block: SiBlock,
    *,
    energy_hartree: float | None,
    selected_input_identity: OrcaSelectedInputScienceIdentity | None = None,
) -> RmsdCandidate:
    """One RMSD candidate with the full executed-provenance comparison key."""
    result = block.result
    comparison_route = result.input_line
    if selected_input_identity is not None:
        comparison_route = json.dumps(
            (
                selected_input_identity.route_tokens,
                selected_input_identity.charge,
                selected_input_identity.multiplicity,
                selected_input_identity.directive_sha256,
                selected_input_identity.atom_sequence,
                selected_input_identity.dependency_identities,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return RmsdCandidate(
        stage_id=stage_id,
        coordinates=tuple(result.coordinates),
        energy_hartree=energy_hartree,
        comparison_key=rmsd_comparison_key(
            formula=result.formula,
            charge=result.charge,
            multiplicity=result.multiplicity,
            method=result.method,
            basis_set=result.basis_set,
            solvation=result.solvation,
            orca_version=result.orca_version,
            input_line=comparison_route,
            electronic_state_verified=result.electronic_state_verified,
        ),
    )


def rmsd_grouping(
    candidates: Sequence[RmsdCandidate],
    rmsd_cfg: Mapping[str, Any] | None,
) -> RmsdGrouping:
    """Group candidates under the durable config, one default source for both ends."""
    cfg = rmsd_cfg or {}
    return group_by_rmsd(
        candidates,
        rmsd_threshold_angstrom=float(
            cfg.get("rmsd_threshold_angstrom") or DEFAULT_RMSD_THRESHOLD_ANGSTROM
        ),
        energy_window_kcal=float(cfg.get("energy_window_kcal") or DEFAULT_RMSD_ENERGY_WINDOW_KCAL),
        heavy_atoms_only=bool(cfg.get("heavy_atoms_only", False)),
    )


__all__ = [
    "GEOMETRY_COMPARISON_EPSILON_ANGSTROM",
    "GEOMETRY_TOL_ANGSTROM",
    "OrcaSelectedInputScienceIdentity",
    "blocks_match_geometry",
    "bound_orca_selected_input_science_identity",
    "coordinates_match",
    "eligible_minimum_block",
    "finite",
    "has_required_provenance",
    "normalized_route_line",
    "orca_science_route_identity",
    "rmsd_candidate_for_block",
    "rmsd_grouping",
    "selected_input_state_matches",
    "unique_single_point_matches",
]
