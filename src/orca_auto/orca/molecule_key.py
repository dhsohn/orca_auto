from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .inp_rewriter import GEOM_HEADER_RE

logger = logging.getLogger(__name__)

TAG_RE = re.compile(r"^#\s*TAG\s*:\s*(.+)$", re.IGNORECASE)
ATOM_LINE_RE = re.compile(r"^\s*([A-Z][a-z]?)\s+[-+]?\d")


@dataclass(frozen=True)
class MoleculeKeyResolution:
    key: str
    source: str


def resolve_molecule_key(inp_path: Path) -> MoleculeKeyResolution:
    tag = _find_user_tag(inp_path)
    if tag is not None:
        return MoleculeKeyResolution(key=tag, source="tag")

    formula = _parse_formula_from_inp(inp_path)
    if formula is not None:
        return MoleculeKeyResolution(key=formula, source="formula")

    return MoleculeKeyResolution(
        key=_directory_name_fallback(inp_path),
        source="directory_fallback",
    )


def _find_user_tag(inp_path: Path) -> str | None:
    try:
        with inp_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                m = TAG_RE.match(line.strip())
                if m:
                    return _sanitize_key(m.group(1).strip())
    except OSError:
        pass
    return None


def _parse_formula_from_inp(inp_path: Path) -> str | None:
    try:
        lines = inp_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None

    for idx, line in enumerate(lines):
        m = GEOM_HEADER_RE.match(line.strip())
        if not m:
            continue

        geom_type = m.group(1).lower()
        if geom_type == "xyzfile":
            filename = m.group(4)
            if not filename:
                return None
            filename = filename.strip().strip('"').strip("'")
            xyz_path = Path(filename)
            if not xyz_path.is_absolute():
                xyz_path = inp_path.parent / xyz_path
            atoms = _parse_xyz_file(xyz_path)
        else:
            atoms = _parse_inline_xyz(lines, idx + 1)

        return _atoms_to_hill_formula(atoms)

    return None


def _parse_inline_xyz(lines: list[str], start: int) -> list[str]:
    atoms: list[str] = []
    for i in range(start, len(lines)):
        stripped = lines[i].strip()
        if stripped == "*":
            break
        m = ATOM_LINE_RE.match(stripped)
        if m:
            atoms.append(m.group(1))
    return atoms


def _parse_xyz_file(xyz_path: Path) -> list[str]:
    atoms: list[str] = []
    try:
        lines = xyz_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        logger.warning("Cannot read xyz file: %s", xyz_path)
        return atoms

    # Standard XYZ format: line 1 = atom count, line 2 = comment, line 3+ = atoms
    for line in lines[2:]:
        m = ATOM_LINE_RE.match(line.strip())
        if m:
            atoms.append(m.group(1))
    return atoms


def _atoms_to_hill_formula(atoms: list[str]) -> str | None:
    if not atoms:
        return None

    counts = Counter(atoms)
    parts: list[str] = []

    if "C" in counts:
        parts.append("C" + (str(counts["C"]) if counts["C"] > 1 else ""))
        del counts["C"]
        if "H" in counts:
            parts.append("H" + (str(counts["H"]) if counts["H"] > 1 else ""))
            del counts["H"]

    for elem in sorted(counts.keys()):
        parts.append(elem + (str(counts[elem]) if counts[elem] > 1 else ""))

    formula = "".join(parts)
    return formula if formula else None


def _sanitize_key(raw: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", raw).strip("_")
    return safe if safe else "unknown"


def _directory_name_fallback(inp_path: Path) -> str:
    name = inp_path.parent.name
    result = _sanitize_key(name)
    return result if result else "unknown"
