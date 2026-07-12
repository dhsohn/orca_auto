"""Post-DFT RMSD re-deduplication of optimized minima.

CREST samples conformers at a semi-empirical level; a DFT re-optimization can
collapse several of those onto the same minimum. This module measures how far
apart two optimized geometries really are (optimal-rotation RMSD) and groups
duplicates so an SI keeps one representative per distinct minimum and records
the degeneracy.

Design constraints (fail-closed toward keeping structures distinct):

* Alignment uses the Horn quaternion method, which optimizes over proper
  rotations only. A naive Kabsch that allows reflections would align a
  structure to its mirror image. Nondegenerate/global reflection-preferred
  pairs are therefore retained, but threshold grouping is still a heuristic
  and can merge nearby local stereochemical variants.
* Atom correspondence is by index. This is valid for CREST-derived conformers
  of one molecule (shared atom ordering) and does not resolve symmetry-
  equivalent permutations (e.g. the two carboxylate oxygens); genuine
  duplicates that differ only by such a relabeling stay distinct, which is the
  safe direction.
* Two candidates merge only when their selected-atom element sequences and
  exact electronic-state/optimization provenance keys match, their
  optimal-rotation RMSD and maximum aligned-atom displacement are both below the
  threshold, and their energies are both present and within the energy window. Grouping is greedy
  from the lowest-energy candidate outward: every member is directly within
  the threshold/window of its representative, so no transitive chain merges
  two structures that are themselves farther apart than the threshold.
* A candidate with no energy is never merged away and never absorbs another.

Pure standard library: no numpy dependency is introduced.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from orca_auto.orca.report.render import KCAL_PER_HARTREE

# An atom row as parsed by the ORCA output reader: (element, x, y, z).
AtomRow = tuple[str, float, float, float]

_HEAVY_EXCLUDED_ELEMENTS = frozenset({"H", "D", "T"})


def _is_heavy(element: str) -> bool:
    return element.strip().capitalize() not in _HEAVY_EXCLUDED_ELEMENTS


def _selected_atoms(coordinates: Sequence[AtomRow], *, heavy_atoms_only: bool) -> list[AtomRow]:
    if not heavy_atoms_only:
        return [(str(el), float(x), float(y), float(z)) for el, x, y, z in coordinates]
    return [
        (str(el), float(x), float(y), float(z)) for el, x, y, z in coordinates if _is_heavy(str(el))
    ]


def _element_signature(atoms: Sequence[AtomRow]) -> tuple[str, ...]:
    return tuple(el.strip().capitalize() for el, *_ in atoms)


def rmsd_comparison_key(
    *,
    formula: str,
    charge: int,
    multiplicity: int,
    method: str,
    basis_set: str,
    solvation: str,
    orca_version: str,
    input_line: str,
    electronic_state_verified: bool,
) -> tuple[object, ...] | None:
    """Exact state/optimization provenance key, or ``None`` when unverified."""
    route = str(input_line).strip()
    version = str(orca_version).strip()
    if not electronic_state_verified or not route or not version or multiplicity < 1:
        return None
    return (
        str(formula).strip(),
        int(charge),
        int(multiplicity),
        str(method).strip(),
        str(basis_set).strip(),
        str(solvation).strip(),
        version,
        route,
    )


def _centered(atoms: Sequence[AtomRow]) -> list[tuple[float, float, float]]:
    n = len(atoms)
    cx = sum(a[1] for a in atoms) / n
    cy = sum(a[2] for a in atoms) / n
    cz = sum(a[3] for a in atoms) / n
    return [(a[1] - cx, a[2] - cy, a[3] - cz) for a in atoms]


def _largest_symmetric_eigenpair(
    matrix: list[list[float]],
) -> tuple[float, tuple[float, ...]]:
    """Largest eigenvalue/vector of a small real symmetric matrix via Jacobi."""
    n = len(matrix)
    a = [list(row) for row in matrix]
    vectors = [[1.0 if row == column else 0.0 for column in range(n)] for row in range(n)]
    for _sweep in range(100):
        off = 0.0
        p, q = 0, 1
        for i in range(n):
            for j in range(i + 1, n):
                if abs(a[i][j]) > off:
                    off = abs(a[i][j])
                    p, q = i, j
        if off <= 1e-15:
            break
        apq = a[p][q]
        theta = (a[q][q] - a[p][p]) / (2.0 * apq)
        t = math.copysign(1.0, theta) / (abs(theta) + math.sqrt(theta * theta + 1.0))
        c = 1.0 / math.sqrt(t * t + 1.0)
        s = t * c
        tau = s / (1.0 + c)
        app, aqq = a[p][p], a[q][q]
        a[p][p] = app - t * apq
        a[q][q] = aqq + t * apq
        a[p][q] = a[q][p] = 0.0
        for i in range(n):
            if i in (p, q):
                continue
            aip, aiq = a[i][p], a[i][q]
            a[i][p] = a[p][i] = aip - s * (aiq + tau * aip)
            a[i][q] = a[q][i] = aiq + s * (aip - tau * aiq)
        for i in range(n):
            vip, viq = vectors[i][p], vectors[i][q]
            vectors[i][p] = c * vip - s * viq
            vectors[i][q] = s * vip + c * viq
    largest = max(range(n), key=lambda index: a[index][index])
    vector = tuple(vectors[row][largest] for row in range(n))
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm == 0.0:
        return math.nan, tuple(math.nan for _ in range(n))
    return a[largest][largest], tuple(value / norm for value in vector)


def _rotate_by_quaternion(
    point: tuple[float, float, float],
    quaternion: tuple[float, ...],
) -> tuple[float, float, float]:
    w, x, y, z = quaternion
    px, py, pz = point
    return (
        (1 - 2 * (y * y + z * z)) * px + 2 * (x * y - z * w) * py + 2 * (x * z + y * w) * pz,
        2 * (x * y + z * w) * px + (1 - 2 * (x * x + z * z)) * py + 2 * (y * z - x * w) * pz,
        2 * (x * z - y * w) * px + 2 * (y * z + x * w) * py + (1 - 2 * (x * x + y * y)) * pz,
    )


def _aligned_metrics(
    a: Sequence[AtomRow],
    b: Sequence[AtomRow],
    *,
    heavy_atoms_only: bool,
) -> tuple[float, float, bool] | None:
    """Proper RMSD, largest displacement, and reflection-preference flag."""
    atoms_a = _selected_atoms(a, heavy_atoms_only=heavy_atoms_only)
    atoms_b = _selected_atoms(b, heavy_atoms_only=heavy_atoms_only)
    if not atoms_a or len(atoms_a) != len(atoms_b):
        return None
    if _element_signature(atoms_a) != _element_signature(atoms_b):
        return None

    p = _centered(atoms_a)
    q = _centered(atoms_b)
    n = len(p)

    r = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
    for (px, py, pz), (qx, qy, qz) in zip(p, q, strict=True):
        pv = (px, py, pz)
        qv = (qx, qy, qz)
        for i in range(3):
            for j in range(3):
                r[i][j] += pv[i] * qv[j]

    r00, r01, r02 = r[0]
    r10, r11, r12 = r[1]
    r20, r21, r22 = r[2]
    determinant = (
        r00 * (r11 * r22 - r12 * r21)
        - r01 * (r10 * r22 - r12 * r20)
        + r02 * (r10 * r21 - r11 * r20)
    )
    # A negative determinant means the optimal unconstrained alignment reverses
    # orientation. Mark even nearly planar full-rank mirror pairs here; the
    # rank-deficient exact-alignment exception is filtered after rotation below.
    orientation_reversing = determinant < 0.0
    f = [
        [r00 + r11 + r22, r12 - r21, r20 - r02, r01 - r10],
        [r12 - r21, r00 - r11 - r22, r01 + r10, r20 + r02],
        [r20 - r02, r01 + r10, -r00 + r11 - r22, r12 + r21],
        [r01 - r10, r20 + r02, r12 + r21, -r00 - r11 + r22],
    ]
    _lambda_max, quaternion = _largest_symmetric_eigenpair(f)
    if any(not math.isfinite(value) for value in quaternion):
        return None

    def deviations(candidate: tuple[float, ...]) -> list[float]:
        return [
            math.dist(_rotate_by_quaternion(left, candidate), right)
            for left, right in zip(p, q, strict=True)
        ]

    forward = deviations(quaternion)
    conjugate = deviations((quaternion[0], -quaternion[1], -quaternion[2], -quaternion[3]))
    distances = min((forward, conjugate), key=lambda values: sum(value * value for value in values))
    if any(not math.isfinite(value) for value in distances):
        return None
    maximum_displacement = max(distances)
    coordinate_scale = max(
        1.0,
        max(abs(value) for points in (p, q) for point in points for value in point),
    )
    # Rank-deficient (for example, two-atom) covariance determinants can acquire
    # a negative round-off sign even for identical coordinates. Only suppress
    # that sign when the proper alignment is numerically indistinguishable from
    # exact; a 1e-6 Å near-planar mirror remains safely separated.
    orientation_reversing = orientation_reversing and maximum_displacement > (
        1e-12 * coordinate_scale
    )
    return (
        math.sqrt(sum(value * value for value in distances) / n),
        maximum_displacement,
        orientation_reversing,
    )


def heavy_atom_rmsd(
    a: Sequence[AtomRow],
    b: Sequence[AtomRow],
    *,
    heavy_atoms_only: bool = False,
) -> float | None:
    """Minimal proper-rotation RMSD between two geometries, or ``None``.

    Returns ``None`` (never a large number) when the two structures cannot be
    compared atom-for-atom by index: different selected-atom counts or a
    different element sequence. That fail-closed contract keeps constitutionally
    different candidates — and reordered atom lists — from ever being merged.
    """
    metrics = _aligned_metrics(a, b, heavy_atoms_only=heavy_atoms_only)
    return metrics[0] if metrics is not None else None


@dataclass(frozen=True)
class RmsdCandidate:
    """One optimized minimum considered for RMSD de-duplication."""

    stage_id: str
    coordinates: tuple[AtomRow, ...]
    energy_hartree: float | None
    comparison_key: tuple[object, ...] | None = None


@dataclass(frozen=True)
class RmsdGroup:
    """A cluster of geometrically degenerate minima, keyed to its representative."""

    representative_stage_id: str
    member_stage_ids: tuple[str, ...]

    @property
    def degeneracy(self) -> int:
        return len(self.member_stage_ids)

    @property
    def merged_stage_ids(self) -> tuple[str, ...]:
        """Non-representative members, deterministically ordered."""
        return tuple(sid for sid in self.member_stage_ids if sid != self.representative_stage_id)


@dataclass(frozen=True)
class RmsdGrouping:
    """The full partition of candidates into RMSD groups (one per representative)."""

    groups: tuple[RmsdGroup, ...] = ()

    def group_for(self, stage_id: str) -> RmsdGroup | None:
        for group in self.groups:
            if stage_id in group.member_stage_ids:
                return group
        return None

    @property
    def representative_ids(self) -> frozenset[str]:
        return frozenset(group.representative_stage_id for group in self.groups)


def group_by_rmsd(
    candidates: Sequence[RmsdCandidate],
    *,
    rmsd_threshold_angstrom: float,
    energy_window_kcal: float,
    heavy_atoms_only: bool = False,
) -> RmsdGrouping:
    """Greedily cluster degenerate minima around their lowest-energy member.

    Candidates are visited lowest energy first (ties broken by ``stage_id`` for
    determinism). Each unassigned candidate becomes a representative and absorbs
    every still-unassigned candidate that is directly within
    ``rmsd_threshold_angstrom`` and ``energy_window_kcal`` of it. A candidate
    with no energy is never merged (kept as its own singleton), so incomplete
    data never drops a structure.
    """
    order = sorted(
        candidates,
        key=lambda c: (
            c.energy_hartree is None or not math.isfinite(c.energy_hartree),
            (
                c.energy_hartree
                if c.energy_hartree is not None and math.isfinite(c.energy_hartree)
                else 0.0
            ),
            c.stage_id,
        ),
    )
    if (
        not math.isfinite(rmsd_threshold_angstrom)
        or rmsd_threshold_angstrom <= 0.0
        or not math.isfinite(energy_window_kcal)
        or energy_window_kcal <= 0.0
    ):
        return RmsdGrouping(
            groups=tuple(
                RmsdGroup(candidate.stage_id, (candidate.stage_id,))
                for candidate in sorted(candidates, key=lambda item: item.stage_id)
            )
        )
    assigned: set[str] = set()
    groups: list[RmsdGroup] = []
    for rep in order:
        if rep.stage_id in assigned:
            continue
        assigned.add(rep.stage_id)
        members = [rep.stage_id]
        if (
            rep.energy_hartree is not None
            and math.isfinite(rep.energy_hartree)
            and rep.comparison_key is not None
        ):
            for other in order:
                if (
                    other.stage_id in assigned
                    or other.energy_hartree is None
                    or not math.isfinite(other.energy_hartree)
                    or other.comparison_key is None
                    or other.comparison_key != rep.comparison_key
                ):
                    continue
                if abs(other.energy_hartree - rep.energy_hartree) * KCAL_PER_HARTREE >= (
                    energy_window_kcal
                ):
                    continue
                metrics = _aligned_metrics(
                    rep.coordinates,
                    other.coordinates,
                    heavy_atoms_only=heavy_atoms_only,
                )
                if metrics is None:
                    continue
                rmsd, maximum_displacement, orientation_reversing = metrics
                if (
                    orientation_reversing
                    or rmsd >= rmsd_threshold_angstrom
                    or maximum_displacement >= rmsd_threshold_angstrom
                ):
                    continue
                members.append(other.stage_id)
                assigned.add(other.stage_id)
        groups.append(
            RmsdGroup(
                representative_stage_id=rep.stage_id,
                member_stage_ids=tuple(sorted(members)),
            )
        )
    return RmsdGrouping(groups=tuple(groups))


__all__ = [
    "AtomRow",
    "RmsdCandidate",
    "RmsdGroup",
    "RmsdGrouping",
    "group_by_rmsd",
    "heavy_atom_rmsd",
    "rmsd_comparison_key",
]
