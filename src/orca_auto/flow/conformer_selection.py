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

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypeGuard

from orca_auto.core.utils.coercion import normalize_text
from orca_auto.orca.input_blocks import file_route_lines, geometry_range
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
) -> RmsdCandidate:
    """One RMSD candidate with the full executed-provenance comparison key."""
    result = block.result
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
            input_line=result.input_line,
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
    "blocks_match_geometry",
    "coordinates_match",
    "eligible_minimum_block",
    "finite",
    "has_required_provenance",
    "normalized_route_line",
    "rmsd_candidate_for_block",
    "rmsd_grouping",
    "selected_input_state_matches",
    "unique_single_point_matches",
]
