"""Convert an xTB Cartesian Hessian into an ORCA ``.hess`` file.

xTB (``--hess``) writes a Turbomole-style ``hessian`` file: a ``$hessian``
header followed by the full 3N x 3N Cartesian Hessian in Hartree/Bohr^2 as a
whitespace-separated float stream. ORCA's ``%geom InHess Read`` expects its own
``.hess`` layout: a ``$hessian`` block in 5-column row-indexed panels plus an
``$atoms`` block with masses and Bohr coordinates matching the input geometry.
"""

from __future__ import annotations

from pathlib import Path

from orca_auto.flow.xyz_utils import load_xyz_frames

ANGSTROM_TO_BOHR = 1.8897261254578281

# Standard atomic weights, sufficient for the elements xTB itself supports
# (GFN methods cover Z = 1..86).
ATOMIC_MASSES: dict[str, float] = {
    "H": 1.008,
    "He": 4.0026,
    "Li": 6.94,
    "Be": 9.0122,
    "B": 10.81,
    "C": 12.011,
    "N": 14.007,
    "O": 15.999,
    "F": 18.998,
    "Ne": 20.180,
    "Na": 22.990,
    "Mg": 24.305,
    "Al": 26.982,
    "Si": 28.085,
    "P": 30.974,
    "S": 32.06,
    "Cl": 35.45,
    "Ar": 39.948,
    "K": 39.098,
    "Ca": 40.078,
    "Sc": 44.956,
    "Ti": 47.867,
    "V": 50.942,
    "Cr": 51.996,
    "Mn": 54.938,
    "Fe": 55.845,
    "Co": 58.933,
    "Ni": 58.693,
    "Cu": 63.546,
    "Zn": 65.38,
    "Ga": 69.723,
    "Ge": 72.630,
    "As": 74.922,
    "Se": 78.971,
    "Br": 79.904,
    "Kr": 83.798,
    "Rb": 85.468,
    "Sr": 87.62,
    "Y": 88.906,
    "Zr": 91.224,
    "Nb": 92.906,
    "Mo": 95.95,
    "Tc": 98.0,
    "Ru": 101.07,
    "Rh": 102.91,
    "Pd": 106.42,
    "Ag": 107.87,
    "Cd": 112.41,
    "In": 114.82,
    "Sn": 118.71,
    "Sb": 121.76,
    "Te": 127.60,
    "I": 126.90,
    "Xe": 131.29,
    "Cs": 132.91,
    "Ba": 137.33,
    "La": 138.91,
    "Ce": 140.12,
    "Pr": 140.91,
    "Nd": 144.24,
    "Pm": 145.0,
    "Sm": 150.36,
    "Eu": 151.96,
    "Gd": 157.25,
    "Tb": 158.93,
    "Dy": 162.50,
    "Ho": 164.93,
    "Er": 167.26,
    "Tm": 168.93,
    "Yb": 173.05,
    "Lu": 174.97,
    "Hf": 178.49,
    "Ta": 180.95,
    "W": 183.84,
    "Re": 186.21,
    "Os": 190.23,
    "Ir": 192.22,
    "Pt": 195.08,
    "Au": 196.97,
    "Hg": 200.59,
    "Tl": 204.38,
    "Pb": 207.2,
    "Bi": 208.98,
    "Po": 209.0,
    "At": 210.0,
    "Rn": 222.0,
}


class HessianConversionError(ValueError):
    """The xTB hessian could not be converted for ORCA consumption."""


def parse_xtb_hessian(hessian_path: str | Path) -> list[list[float]]:
    """Parse a Turbomole-style xTB ``hessian`` file into a square matrix."""
    path = Path(hessian_path)
    try:
        tokens = path.read_text(encoding="utf-8", errors="ignore").split()
    except OSError as exc:
        raise HessianConversionError(f"Cannot read xTB hessian file: {path}") from exc
    values: list[float] = []
    for token in tokens:
        if token.startswith("$"):
            continue
        try:
            values.append(float(token))
        except ValueError as exc:
            raise HessianConversionError(
                f"Non-numeric token {token!r} in xTB hessian file: {path}"
            ) from exc
    if not values:
        raise HessianConversionError(f"Empty xTB hessian file: {path}")
    dimension = round(len(values) ** 0.5)
    if dimension * dimension != len(values) or dimension % 3 != 0:
        raise HessianConversionError(
            f"xTB hessian is not a square 3N matrix: {len(values)} values in {path}"
        )
    return [values[row * dimension : (row + 1) * dimension] for row in range(dimension)]


def _parse_xyz_atoms(xyz_path: str | Path) -> list[tuple[str, float, float, float]]:
    frames = load_xyz_frames(xyz_path)
    if not frames:
        raise HessianConversionError(f"No geometry frames in xyz file: {xyz_path}")
    atoms: list[tuple[str, float, float, float]] = []
    for line in frames[0].atom_lines:
        parts = line.split()
        if len(parts) < 4:
            raise HessianConversionError(f"Malformed atom line {line!r} in {xyz_path}")
        symbol = parts[0].capitalize()
        try:
            x, y, z = (float(part) for part in parts[1:4])
        except ValueError as exc:
            raise HessianConversionError(
                f"Non-numeric coordinates in atom line {line!r} in {xyz_path}"
            ) from exc
        atoms.append((symbol, x, y, z))
    return atoms


def _render_hessian_block(matrix: list[list[float]]) -> list[str]:
    dimension = len(matrix)
    lines = ["$hessian", str(dimension)]
    for block_start in range(0, dimension, 5):
        columns = range(block_start, min(block_start + 5, dimension))
        lines.append("".join(f"{column:>19d}" for column in columns))
        for row in range(dimension):
            values = "".join(f"  {matrix[row][column]: .10E}" for column in columns)
            lines.append(f"{row:>7d}{values}")
    return lines


def _render_atoms_block(atoms: list[tuple[str, float, float, float]]) -> list[str]:
    lines = ["$atoms", str(len(atoms))]
    for symbol, x, y, z in atoms:
        mass = ATOMIC_MASSES.get(symbol)
        if mass is None:
            raise HessianConversionError(f"No atomic mass known for element {symbol!r}")
        lines.append(
            f" {symbol:<4s} {mass:>10.5f}"
            f"   {x * ANGSTROM_TO_BOHR: .10f}   {y * ANGSTROM_TO_BOHR: .10f}"
            f"   {z * ANGSTROM_TO_BOHR: .10f}"
        )
    return lines


def write_orca_hess_from_xtb(
    *,
    xtb_hessian_path: str | Path,
    xyz_path: str | Path,
    target_path: str | Path,
) -> Path:
    """Write an ORCA ``.hess`` file from an xTB hessian and its xyz geometry.

    The xyz file must hold the same geometry (atom count and order) the xTB
    hessian was computed for; coordinates are converted from Angstrom to Bohr.
    """
    matrix = parse_xtb_hessian(xtb_hessian_path)
    atoms = _parse_xyz_atoms(xyz_path)
    if len(matrix) != 3 * len(atoms):
        raise HessianConversionError(
            f"Hessian dimension {len(matrix)} does not match 3 x {len(atoms)} atoms "
            f"(hessian={xtb_hessian_path}, xyz={xyz_path})"
        )
    lines = [
        "$orca_hessian_file",
        "",
        *_render_hessian_block(matrix),
        "",
        *_render_atoms_block(atoms),
        "",
        "$end",
        "",
    ]
    target = Path(target_path)
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


__all__ = [
    "ANGSTROM_TO_BOHR",
    "ATOMIC_MASSES",
    "HessianConversionError",
    "parse_xtb_hessian",
    "write_orca_hess_from_xtb",
]
