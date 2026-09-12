"""Sanity-check a path-search TS guess against its reactant/product endpoints.

xTB path searches sometimes hand back a "TS" that is chemically mangled: the
molecule dissociated mid-path, or bonds far away from the reaction center were
scrambled. Such guesses waste an expensive ORCA OptTS attempt each, so they are
screened before ORCA stages are materialized.

Two structure-only rules, calibrated on real TS8_wf output where they separated
the two viable guesses from all seven mangled ones exactly:

1. Fragmentation: the TS guess must not have more connected fragments than
   either endpoint. A bimolecular path that falls apart into isolated molecules
   has no reacting contact and cannot relax to a saddle point. A forming or
   breaking bond still counts as a connection while it is merely elongated
   (within ``reacting_bond_stretch_scale``), so an atom mid-transfer between two
   partners is not mistaken for a loose fragment.
2. Spurious rearrangement: bonds that differ between the TS guess and an
   endpoint must belong to the reaction's own bond set (bonds that differ
   between reactant and product). Bond changes outside that set mean the path
   scrambled spectator atoms.

All three geometries must share one atom ordering, which ``xtb --path``
requires of its inputs anyway.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.flow.xyz_utils import load_xyz_frames

# Pyykko covalent radii (Angstrom) for elements GFN methods commonly see.
_COVALENT_RADII: dict[str, float] = {
    "H": 0.32,
    "He": 0.46,
    "Li": 1.33,
    "Be": 1.02,
    "B": 0.85,
    "C": 0.75,
    "N": 0.71,
    "O": 0.63,
    "F": 0.64,
    "Ne": 0.67,
    "Na": 1.55,
    "Mg": 1.39,
    "Al": 1.26,
    "Si": 1.16,
    "P": 1.11,
    "S": 1.03,
    "Cl": 0.99,
    "Ar": 0.96,
    "K": 1.96,
    "Ca": 1.71,
    "Sc": 1.48,
    "Ti": 1.36,
    "V": 1.34,
    "Cr": 1.22,
    "Mn": 1.19,
    "Fe": 1.16,
    "Co": 1.11,
    "Ni": 1.10,
    "Cu": 1.12,
    "Zn": 1.18,
    "Ga": 1.24,
    "Ge": 1.21,
    "As": 1.21,
    "Se": 1.16,
    "Br": 1.14,
    "Kr": 1.17,
    "Rb": 2.10,
    "Sr": 1.85,
    "Y": 1.63,
    "Zr": 1.54,
    "Nb": 1.47,
    "Mo": 1.38,
    "Tc": 1.28,
    "Ru": 1.25,
    "Rh": 1.25,
    "Pd": 1.20,
    "Ag": 1.28,
    "Cd": 1.36,
    "In": 1.42,
    "Sn": 1.40,
    "Sb": 1.40,
    "Te": 1.36,
    "I": 1.33,
    "Xe": 1.31,
    "Cs": 2.32,
    "Ba": 1.96,
    "La": 1.80,
    "Hf": 1.52,
    "Ta": 1.46,
    "W": 1.37,
    "Re": 1.31,
    "Os": 1.29,
    "Ir": 1.22,
    "Pt": 1.23,
    "Au": 1.24,
    "Hg": 1.33,
    "Tl": 1.44,
    "Pb": 1.44,
    "Bi": 1.51,
}
_FALLBACK_RADIUS = 1.5
DEFAULT_BOND_SCALE = 1.25
DEFAULT_MAX_SPURIOUS_BOND_CHANGES = 0
# A forming/breaking bond is elongated at a saddle point but not severed. In the
# TS8_wf path searches the viable guesses stretched a reacting bond to at most
# 1.49x the covalent-radius sum, while the dissociated ones sat at 2.2-4.5x.
DEFAULT_REACTING_BOND_STRETCH_SCALE = 1.75


class GeometryValidationError(ValueError):
    """The geometries could not be compared (unreadable or inconsistent)."""


@dataclass(frozen=True)
class TsGuessVerdict:
    valid: bool
    reasons: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "reasons": list(self.reasons),
            **self.metrics,
        }


def _read_atoms(xyz_path: str | Path) -> list[tuple[str, float, float, float]]:
    frames = load_xyz_frames(xyz_path)
    if not frames:
        raise GeometryValidationError(f"No geometry frames in xyz file: {xyz_path}")
    atoms: list[tuple[str, float, float, float]] = []
    for line in frames[0].atom_lines:
        parts = line.split()
        if len(parts) < 4:
            raise GeometryValidationError(f"Malformed atom line {line!r} in {xyz_path}")
        try:
            x, y, z = (float(part) for part in parts[1:4])
        except ValueError as exc:
            raise GeometryValidationError(
                f"Non-numeric coordinates in atom line {line!r} in {xyz_path}"
            ) from exc
        if not all(math.isfinite(value) for value in (x, y, z)):
            raise GeometryValidationError(
                f"Non-finite coordinates in atom line {line!r} in {xyz_path}"
            )
        atoms.append((parts[0].capitalize(), x, y, z))
    return atoms


def _bond_set(
    atoms: list[tuple[str, float, float, float]], *, bond_scale: float
) -> frozenset[tuple[int, int]]:
    bonds: set[tuple[int, int]] = set()
    for i, (symbol_i, xi, yi, zi) in enumerate(atoms):
        radius_i = _COVALENT_RADII.get(symbol_i, _FALLBACK_RADIUS)
        for j in range(i + 1, len(atoms)):
            symbol_j, xj, yj, zj = atoms[j]
            cutoff = bond_scale * (radius_i + _COVALENT_RADII.get(symbol_j, _FALLBACK_RADIUS))
            if math.dist((xi, yi, zi), (xj, yj, zj)) < cutoff:
                bonds.add((i, j))
    return frozenset(bonds)


def _stretched_reacting_bonds(
    atoms: list[tuple[str, float, float, float]],
    reaction_bonds: frozenset[tuple[int, int]],
    *,
    stretch_scale: float,
) -> frozenset[tuple[int, int]]:
    """Reacting bonds that are merely elongated in this geometry, not severed."""
    stretched: set[tuple[int, int]] = set()
    for i, j in reaction_bonds:
        symbol_i, xi, yi, zi = atoms[i]
        symbol_j, xj, yj, zj = atoms[j]
        cutoff = stretch_scale * (
            _COVALENT_RADII.get(symbol_i, _FALLBACK_RADIUS)
            + _COVALENT_RADII.get(symbol_j, _FALLBACK_RADIUS)
        )
        if math.dist((xi, yi, zi), (xj, yj, zj)) < cutoff:
            stretched.add((i, j))
    return frozenset(stretched)


def _fragment_count(atom_count: int, bonds: frozenset[tuple[int, int]]) -> int:
    adjacency: dict[int, set[int]] = {index: set() for index in range(atom_count)}
    for i, j in bonds:
        adjacency[i].add(j)
        adjacency[j].add(i)
    seen: set[int] = set()
    fragments = 0
    for start in range(atom_count):
        if start in seen:
            continue
        fragments += 1
        stack = [start]
        while stack:
            atom = stack.pop()
            if atom in seen:
                continue
            seen.add(atom)
            stack.extend(adjacency[atom] - seen)
    return fragments


def validate_ts_guess_geometry(
    *,
    ts_guess_xyz: str | Path,
    reactant_xyz: str | Path,
    product_xyz: str | Path,
    bond_scale: float = DEFAULT_BOND_SCALE,
    max_spurious_bond_changes: int = DEFAULT_MAX_SPURIOUS_BOND_CHANGES,
    reacting_bond_stretch_scale: float = DEFAULT_REACTING_BOND_STRETCH_SCALE,
) -> TsGuessVerdict:
    """Judge whether a path-search TS guess is structurally plausible.

    Raises :class:`GeometryValidationError` when the files cannot be compared
    (missing/corrupt geometry, mismatched atom counts or element orders);
    callers should treat that as "unknown", not as invalid.
    """
    ts_atoms = _read_atoms(ts_guess_xyz)
    reactant_atoms = _read_atoms(reactant_xyz)
    product_atoms = _read_atoms(product_xyz)
    for label, atoms in (("reactant", reactant_atoms), ("product", product_atoms)):
        if len(atoms) != len(ts_atoms):
            raise GeometryValidationError(
                f"Atom count mismatch: ts_guess has {len(ts_atoms)}, {label} has {len(atoms)}"
            )
        if any(a[0] != b[0] for a, b in zip(atoms, ts_atoms, strict=True)):
            raise GeometryValidationError(f"Element order mismatch between ts_guess and {label}")

    ts_bonds = _bond_set(ts_atoms, bond_scale=bond_scale)
    reactant_bonds = _bond_set(reactant_atoms, bond_scale=bond_scale)
    product_bonds = _bond_set(product_atoms, bond_scale=bond_scale)
    reaction_bonds = reactant_bonds ^ product_bonds

    spurious_vs_reactant = len((ts_bonds ^ reactant_bonds) - reaction_bonds)
    spurious_vs_product = len((ts_bonds ^ product_bonds) - reaction_bonds)
    spurious = min(spurious_vs_reactant, spurious_vs_product)

    # A mid-transfer atom can sit past the bond cutoff of both partners without
    # the molecule having come apart, so reacting bonds count as connections --
    # but only while still within reach. Adding every reacting bond regardless of
    # distance would reconnect a genuinely dissociated guess and hide it.
    stretched_bonds = _stretched_reacting_bonds(
        ts_atoms,
        reaction_bonds,
        stretch_scale=max(bond_scale, reacting_bond_stretch_scale),
    )
    ts_fragments = _fragment_count(len(ts_atoms), ts_bonds | stretched_bonds)
    endpoint_max_fragments = max(
        _fragment_count(len(reactant_atoms), reactant_bonds),
        _fragment_count(len(product_atoms), product_bonds),
    )

    reasons: list[str] = []
    if ts_fragments > endpoint_max_fragments:
        reasons.append(
            f"fragmented: ts_guess splits into {ts_fragments} fragments "
            f"but the endpoints have at most {endpoint_max_fragments}"
        )
    if spurious > max_spurious_bond_changes:
        reasons.append(
            f"rearranged: {spurious} bond changes outside the "
            f"{len(reaction_bonds)} reacting bonds (allowed {max_spurious_bond_changes})"
        )

    return TsGuessVerdict(
        valid=not reasons,
        reasons=tuple(reasons),
        metrics={
            "bond_scale": bond_scale,
            "max_spurious_bond_changes": max_spurious_bond_changes,
            "reacting_bond_stretch_scale": reacting_bond_stretch_scale,
            "reaction_bond_count": len(reaction_bonds),
            "intact_reacting_bond_count": len(stretched_bonds),
            "spurious_bond_changes_vs_reactant": spurious_vs_reactant,
            "spurious_bond_changes_vs_product": spurious_vs_product,
            "ts_fragments": ts_fragments,
            "endpoint_max_fragments": endpoint_max_fragments,
        },
    )


__all__ = [
    "DEFAULT_BOND_SCALE",
    "DEFAULT_MAX_SPURIOUS_BOND_CHANGES",
    "DEFAULT_REACTING_BOND_STRETCH_SCALE",
    "GeometryValidationError",
    "TsGuessVerdict",
    "validate_ts_guess_geometry",
]
