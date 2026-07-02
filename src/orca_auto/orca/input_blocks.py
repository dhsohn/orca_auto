from __future__ import annotations

import re
from pathlib import Path

GEOM_HEADER_RE = re.compile(
    r"^\s*\*\s+(xyzfile|xyz)\s+(-?\d+)\s+(\d+)(?:\s+(.*))?$",
    re.IGNORECASE,
)
BLOCK_START_RE = re.compile(r"^\s*%([A-Za-z0-9_\-]+)")
MOINP_RE = re.compile(r"^\s*%moinp\b", re.IGNORECASE)
NESTED_BLOCK_NAMES = {"scan", "constraints"}


def find_route_idx(lines: list[str]) -> int | None:
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("!"):
            return idx
    return None


def ensure_route_keywords(lines: list[str], keywords: list[str]) -> bool:
    idx = find_route_idx(lines)
    if idx is None:
        lines.insert(0, "! " + " ".join(keywords))
        return True

    current = lines[idx].strip()
    token_set = {tok.upper() for tok in current[1:].split()}
    missing = [kw for kw in keywords if kw.upper() not in token_set]
    if not missing:
        return False
    lines[idx] = current + " " + " ".join(missing)
    return True


def find_geometry_start(lines: list[str]) -> int | None:
    for idx, line in enumerate(lines):
        if GEOM_HEADER_RE.match(line.strip()):
            return idx
    return None


def find_block_range(lines: list[str], block_name: str) -> tuple[int, int, bool] | None:
    name = block_name.lower()
    for i, line in enumerate(lines):
        m = BLOCK_START_RE.match(line)
        if not m:
            continue
        if m.group(1).lower() != name:
            continue
        nested_depth = 0
        for j in range(i + 1, len(lines)):
            stripped = lines[j].strip().lower()
            if stripped in NESTED_BLOCK_NAMES:
                nested_depth += 1
                continue
            if stripped == "end":
                if nested_depth > 0:
                    nested_depth -= 1
                    continue
                return i, j, False
        return i, len(lines), True
    return None


def set_block_key_value(lines: list[str], block_name: str, key: str, value: str) -> bool:
    rng = find_block_range(lines, block_name)
    key_lower = key.lower()

    if rng is None:
        insert_at = find_geometry_start(lines)
        if insert_at is None:
            insert_at = len(lines)
        block = [f"%{block_name}", f"  {key} {value}", "end", ""]
        lines[insert_at:insert_at] = block
        return True

    start, end, needs_close = rng
    if needs_close:
        lines.insert(end, "end")
    changed = False
    replaced = False
    for i in range(start + 1, end):
        stripped = lines[i].strip()
        if not stripped:
            continue
        tokens = stripped.split()
        if tokens and tokens[0].lower() == key_lower:
            new_line = f"  {key} {value}"
            if lines[i] != new_line:
                lines[i] = new_line
                changed = True
            replaced = True

    if not replaced:
        lines.insert(end, f"  {key} {value}")
        changed = True
    return changed


def format_relative_or_absolute(path: Path, base_dir: Path) -> str:
    resolved = path.resolve()
    base_resolved = base_dir.resolve()
    try:
        ref = resolved.relative_to(base_resolved)
    except ValueError:
        ref = resolved
    return str(ref).replace("\\", "/")


def quote_orca_path(path_text: str) -> str:
    escaped = path_text.replace('"', '\\"')
    return f'"{escaped}"'


def set_moinp(lines: list[str], checkpoint: Path, base_dir: Path) -> bool:
    ref = quote_orca_path(format_relative_or_absolute(checkpoint, base_dir))
    new_line = f"%moinp {ref}"
    for idx, line in enumerate(lines):
        if not MOINP_RE.match(line):
            continue
        if lines[idx].strip() == new_line:
            return False
        lines[idx] = new_line
        return True

    insert_at = find_geometry_start(lines)
    if insert_at is None:
        insert_at = len(lines)
    lines.insert(insert_at, new_line)
    return True


def geometry_range(lines: list[str]) -> tuple[int, int, int, int] | None:
    for start, line in enumerate(lines):
        m = GEOM_HEADER_RE.match(line.strip())
        if not m:
            continue
        geom_type = m.group(1).lower()
        charge = int(m.group(2))
        mult = int(m.group(3))
        if geom_type == "xyzfile":
            return start, start + 1, charge, mult
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].strip() == "*":
                end = i + 1
                break
        return start, end, charge, mult
    return None


def replace_geometry_with_xyzfile(lines: list[str], geom_file: Path, base_dir: Path) -> bool:
    geo = geometry_range(lines)
    if geo is None:
        return False
    start, end, charge, mult = geo
    geom_resolved = geom_file.resolve()
    base_resolved = base_dir.resolve()
    try:
        rel = geom_resolved.relative_to(base_resolved)
    except ValueError:
        rel = geom_resolved
    ref = str(rel).replace("\\", "/")
    if " " in ref:
        ref = f'"{ref}"'
    lines[start:end] = [f"* xyzfile {charge} {mult} {ref}"]
    return True
