from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

from .input_blocks import find_block_range, replace_geometry_with_xyzfile
from .resource_directives import clamp_maxcore_to_budget

SCANTS_ROUTE_RE = re.compile(r"\bSCANTS\b", re.IGNORECASE)
_FLOAT_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")
_GEOM_SCAN_START_RE = re.compile(r"^\s*scan\s*$", re.IGNORECASE)
_GEOM_END_RE = re.compile(r"^\s*end\s*$", re.IGNORECASE)
_SCANTS_OPTTS_REFINEMENT_MARKERS = (
    "REFINING TS GUESS STRUCTURE",
    "REFINING THE TS GUESS STRUCTURE",
)
_SIMPLE_SCAN_COORD_LINE_RE = re.compile(
    rf"^(?P<prefix>\s*\S+(?:\s+\d+)+\s*=\s*)"
    rf"(?P<start>{_FLOAT_RE.pattern})(?P<sep1>\s*,\s*)"
    rf"(?P<end>{_FLOAT_RE.pattern})(?P<sep2>\s*,\s*)"
    r"(?P<points>\d+)(?P<suffix>.*)$"
)


@dataclass(frozen=True)
class ScanTSSurfacePoint:
    index: int
    coordinates: tuple[float, ...]
    energy: float


def input_uses_scants(inp_path: Path) -> bool:
    try:
        for line in inp_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("!"):
                return bool(SCANTS_ROUTE_RE.search(stripped))
            return False
    except OSError:
        return False
    return False


def parse_scants_actual_surface(out_path: Path) -> list[ScanTSSurfacePoint]:
    points: list[ScanTSSurfacePoint] = []
    in_actual_surface = False
    try:
        with out_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                upper = line.upper()
                if "THE CALCULATED SURFACE USING THE 'ACTUAL ENERGY'" in upper:
                    in_actual_surface = True
                    continue
                if not in_actual_surface:
                    continue
                if "THE CALCULATED SURFACE USING THE SCF ENERGY" in upper:
                    break
                values = [float(match.group(0)) for match in _FLOAT_RE.finditer(line)]
                if len(values) < 2:
                    continue
                points.append(
                    ScanTSSurfacePoint(
                        index=len(points) + 1,
                        coordinates=tuple(values[:-1]),
                        energy=values[-1],
                    )
                )
    except OSError:
        return []
    return points


def highest_scants_surface_point(out_path: Path) -> ScanTSSurfacePoint | None:
    points = parse_scants_actual_surface(out_path)
    if not points:
        return None
    return max(points, key=lambda point: (point.energy, -point.index))


def scants_guess_xyz_for_output(source_inp: Path, out_path: Path) -> Path | None:
    point = highest_scants_surface_point(out_path)
    if point is None:
        return None
    candidate = source_inp.with_name(f"{source_inp.stem}.{point.index:03d}.xyz")
    try:
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    except OSError:
        return None
    return None


def output_indicates_scants_optts_refinement(out_path: Path) -> bool:
    try:
        with out_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                upper = line.upper()
                if any(marker in upper for marker in _SCANTS_OPTTS_REFINEMENT_MARKERS):
                    return True
    except OSError:
        return False
    return False


def _same_stem_xyz(source_inp: Path) -> Path | None:
    candidate = source_inp.with_suffix(".xyz")
    try:
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    except OSError:
        return None
    return None


def scants_optts_resume_guess_xyz_for_output(source_inp: Path, out_path: Path) -> Path | None:
    if output_indicates_scants_optts_refinement(out_path):
        refinement_xyz = _same_stem_xyz(source_inp)
        if refinement_xyz is not None:
            return refinement_xyz
    return scants_guess_xyz_for_output(source_inp, out_path)


def highest_numbered_scan_xyz_index(source_inp: Path) -> int | None:
    pattern = re.compile(rf"^{re.escape(source_inp.stem)}\.(\d{{3}})\.xyz$")
    best: int | None = None
    for candidate in source_inp.parent.glob("*.xyz"):
        match = pattern.match(candidate.name)
        if match is None:
            continue
        try:
            if candidate.stat().st_size <= 0:
                continue
        except OSError:
            continue
        index = int(match.group(1))
        if best is None or index > best:
            best = index
    return best


def apply_scants_relaxed_scan_resume_rewrite(lines: list[str], source_inp: Path) -> List[str]:
    last_index = highest_numbered_scan_xyz_index(source_inp)
    if last_index is None:
        return []

    scan_line_indices = _simple_scan_coord_line_indices(lines)
    if not scan_line_indices:
        return []
    if not _scan_lines_share_total_points(lines, scan_line_indices):
        return []

    rewritten_lines: list[tuple[int, str]] = []
    for line_idx in scan_line_indices:
        rewritten = _resume_simple_scan_line(lines[line_idx], completed_points=last_index)
        if rewritten is None or rewritten == lines[line_idx]:
            return []
        rewritten_lines.append((line_idx, rewritten))

    for line_idx, rewritten in rewritten_lines:
        lines[line_idx] = rewritten
    return [f"scants_scan_range_resumed_after_point_{last_index:03d}"]


def _simple_scan_coord_line_indices(lines: list[str]) -> list[int]:
    block = find_block_range(lines, "geom")
    if block is None:
        return []
    start, end, _needs_close = block
    indices: list[int] = []
    i = start + 1
    while i < end:
        if not _GEOM_SCAN_START_RE.match(lines[i]):
            i += 1
            continue
        scan_end = _scan_subblock_end(lines, i + 1, end)
        for idx in range(i + 1, scan_end - 1):
            if _SIMPLE_SCAN_COORD_LINE_RE.match(lines[idx]):
                indices.append(idx)
        i = scan_end
    return indices


def _scan_lines_share_total_points(lines: list[str], indices: list[int]) -> bool:
    totals: set[int] = set()
    for idx in indices:
        match = _SIMPLE_SCAN_COORD_LINE_RE.match(lines[idx])
        if match is None:
            return False
        totals.add(int(match.group("points")))
    return len(totals) == 1


def _resume_simple_scan_line(line: str, *, completed_points: int) -> str | None:
    match = _SIMPLE_SCAN_COORD_LINE_RE.match(line)
    if match is None:
        return None
    total_points = int(match.group("points"))
    if completed_points <= 0 or completed_points >= total_points or total_points <= 1:
        return None

    start = float(match.group("start"))
    end = float(match.group("end"))
    step = (end - start) / (total_points - 1)
    next_start = start + step * completed_points
    remaining_points = total_points - completed_points
    return (
        f"{match.group('prefix')}{_format_scan_float(next_start)}"
        f"{match.group('sep1')}{match.group('end')}"
        f"{match.group('sep2')}{remaining_points}{match.group('suffix')}"
    )


def _format_scan_float(value: float) -> str:
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    if text == "-0":
        return "0"
    return text


def prepare_scants_optts_fallback_input(
    *,
    source_inp: Path,
    target_inp: Path,
    reaction_dir: Path,
    out_path: Path,
    max_memory_gb: int | None = None,
) -> tuple[Path | None, List[str]]:
    if not input_uses_scants(source_inp):
        return None, []

    guess_xyz = scants_guess_xyz_for_output(source_inp, out_path)
    if guess_xyz is None:
        return None, []

    lines = source_inp.read_text(encoding="utf-8", errors="ignore").splitlines()
    actions: List[str] = []

    if _replace_scants_route_with_optts(lines):
        actions.append("scants_fallback_to_optts")
    if _remove_geom_scan_subblock(lines):
        actions.append("scants_scan_block_removed")
    if replace_geometry_with_xyzfile(lines, guess_xyz, target_inp.parent):
        actions.append(f"scants_guess_from_{guess_xyz.name}")
    else:
        return None, []

    if max_memory_gb is not None and clamp_maxcore_to_budget(lines, max_memory_gb=max_memory_gb):
        actions.append("maxcore_clamped_to_budget")

    target_inp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target_inp, actions


def apply_scants_optts_resume_rewrite(
    *,
    lines: list[str],
    source_inp: Path,
    target_inp: Path,
    out_path: Path,
) -> List[str]:
    guess_xyz = scants_optts_resume_guess_xyz_for_output(source_inp, out_path)
    if guess_xyz is None:
        return []

    actions: List[str] = []
    if _replace_scants_route_with_optts(lines):
        actions.append("scants_resume_to_optts")
    if _remove_geom_scan_subblock(lines):
        actions.append("scants_scan_block_removed")
    if replace_geometry_with_xyzfile(lines, guess_xyz, target_inp.parent):
        actions.append(f"geometry_restart_from_{guess_xyz.name}")
    else:
        return []
    return actions


def _replace_scants_route_with_optts(lines: list[str]) -> bool:
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not stripped.startswith("!"):
            return False
        tokens = stripped[1:].split()
        if not any(token.upper() == "SCANTS" for token in tokens):
            return False
        rewritten: list[str] = []
        inserted_optts = False
        for token in tokens:
            if token.upper() == "SCANTS":
                if not inserted_optts:
                    rewritten.append("OPTTS")
                    inserted_optts = True
                continue
            rewritten.append(token)
        lines[idx] = "! " + " ".join(rewritten)
        return True
    return False


def _remove_geom_scan_subblock(lines: list[str]) -> bool:
    block = find_block_range(lines, "geom")
    if block is None:
        return False
    start, end, _needs_close = block
    changed = False
    i = start + 1
    while i < end:
        if not _GEOM_SCAN_START_RE.match(lines[i]):
            i += 1
            continue
        remove_until = _scan_subblock_end(lines, i + 1, end)
        del lines[i:remove_until]
        end -= remove_until - i
        changed = True
    return changed


def _scan_subblock_end(lines: list[str], start: int, stop: int) -> int:
    for idx in range(start, stop):
        if _GEOM_END_RE.match(lines[idx]):
            return idx + 1
    return stop
