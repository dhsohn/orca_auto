"""Resolve a job's molecule key (user ``# TAG``, Hill formula, or directory name)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .input_blocks import OrcaGeometryBlock, find_geometry_block
from .orca_chemistry import build_formula

logger = logging.getLogger(__name__)

TAG_RE = re.compile(r"^#\s*TAG\s*:\s*(.+)$", re.IGNORECASE)
ATOM_LINE_RE = re.compile(r"^\s*([A-Z][a-z]?)\s+[-+]?\d")


@dataclass(frozen=True)
class MoleculeKeyResolution:
    key: str
    source: str


def resolve_molecule_key(inp_path: Path) -> MoleculeKeyResolution:
    try:
        text = inp_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        text = ""
    return molecule_key_from_text(text, inp_path)


def molecule_key_from_text(text: str, inp_path: Path) -> MoleculeKeyResolution:
    """The key of an input whose text is ``text``; ``inp_path`` places its xyzfile and folder."""
    tag = _find_user_tag(text)
    if tag is not None:
        return MoleculeKeyResolution(key=tag, source="tag")

    formula = _formula_from_lines(text.splitlines(), inp_path.parent)
    if formula is not None:
        return MoleculeKeyResolution(key=formula, source="formula")

    return MoleculeKeyResolution(
        key=_directory_name_fallback(inp_path),
        source="directory_fallback",
    )


def _find_user_tag(text: str) -> str | None:
    # A tag line ends only where a text-mode read ends it ("\n", "\r\n" or
    # "\r"): a form feed or another str.splitlines() break stays in the tag.
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = TAG_RE.match(line.strip())
        if m:
            return _sanitize_key(m.group(1).strip())
    return None


def _formula_from_lines(lines: list[str], inp_dir: Path) -> str | None:
    # The shared geometry scanner reads the header, the xyzfile reference and
    # the inline atom rows through the ORCA comment tokenizer, so this key
    # sees the same atoms as execution binding does.
    block = find_geometry_block(lines)
    if block is None:
        return None
    if block.kind == "xyzfile":
        if not block.reference:
            return None
        xyz_path = Path(block.reference)
        if not xyz_path.is_absolute():
            xyz_path = inp_dir / xyz_path
        atoms = _parse_xyz_file(xyz_path)
    else:
        atoms = _parse_inline_xyz(block)
    # Queue rows, job_locations.json and the companion app key on this string.
    # Molecule keys have always been Hill formulas; build_formula is that rule.
    return build_formula(atoms) or None


def _parse_inline_xyz(block: OrcaGeometryBlock) -> list[str]:
    # A silently skipped line would yield a plausible but wrong formula, so a
    # non-atom row inside the geometry block fails closed to "no formula"
    # (the caller then falls back to the directory-name key). Comment-only
    # lines are not rows: the tokenizer already dropped them.
    atoms: list[str] = []
    for _index, text in block.atom_rows:
        m = ATOM_LINE_RE.match(text)
        if m is None:
            logger.warning("Unparseable inline geometry line for molecule key")
            return []
        atoms.append(m.group(1))
    return atoms


def _parse_xyz_file(xyz_path: Path) -> list[str]:
    try:
        lines = xyz_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        logger.warning("Cannot read xyz file: %s", xyz_path)
        return []

    # Standard XYZ format: line 1 = atom count, line 2 = comment, line 3+ =
    # exactly that many atom lines. A truncated or corrupt file must yield no
    # formula rather than a plausible subset of the real one.
    try:
        declared = int(lines[0].strip())
    except (IndexError, ValueError):
        logger.warning("Invalid XYZ atom-count header for molecule key: %s", xyz_path)
        return []
    atom_lines = lines[2 : 2 + declared]
    if declared <= 0 or len(atom_lines) != declared:
        logger.warning("XYZ file is truncated for molecule key: %s", xyz_path)
        return []
    atoms: list[str] = []
    for line in atom_lines:
        m = ATOM_LINE_RE.match(line.strip())
        if m is None:
            logger.warning("Unparseable XYZ atom line for molecule key: %s", xyz_path)
            return []
        atoms.append(m.group(1))
    return atoms


def _sanitize_key(raw: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", raw).strip("_")
    return safe if safe else "unknown"


def _directory_name_fallback(inp_path: Path) -> str:
    name = inp_path.parent.name
    result = _sanitize_key(name)
    return result if result else "unknown"
