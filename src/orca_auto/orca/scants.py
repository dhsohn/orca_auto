from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

from .input_blocks import (
    GEOM_HEADER_RE,
    MOINP_RE,
    find_block_range,
    find_route_idx,
    replace_geometry_with_xyzfile,
)
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


def highest_numbered_scan_xyz(source_inp: Path) -> tuple[int, Path] | None:
    index = highest_numbered_scan_xyz_index(source_inp)
    if index is None:
        return None
    candidate = source_inp.with_name(f"{source_inp.stem}.{index:03d}.xyz")
    try:
        if candidate.exists() and candidate.stat().st_size > 0:
            return index, candidate
    except OSError:
        return None
    return None


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


def apply_scants_failed_scan_retry_rewrite(
    lines: list[str],
    *,
    retry_number: int,
    source_inp: Path | None = None,
    target_inp: Path | None = None,
) -> List[str]:
    """Rewrite failed ScanTS retries without checkpoint/last-geometry restarts.

    ScanTS failures can leave the same-stem ``*.xyz`` and ``*.gbw`` in a chemically
    bad state. Failed-attempt retries therefore preserve the input geometry from
    the source input and change the ScanTS recipe itself instead of adding MORead
    or replacing the geometry with the latest attempt artifact.
    """
    cleanup_actions = _remove_checkpoint_restart_directives(lines)
    recipe_actions: List[str] = []

    if retry_number < 1:
        return []
    if source_inp is None or target_inp is None:
        return []
    recipe_actions.extend(
        _continue_simple_scan_from_last_numbered_xyz(
            lines,
            source_inp=source_inp,
            target_inp=target_inp,
            min_extension_steps=6,
            extension_fraction=0.20,
        )
    )

    if not recipe_actions:
        return []
    actions = cleanup_actions + recipe_actions
    if not any(action.startswith("geometry_restart_from_") for action in recipe_actions):
        actions.append("scants_retry_preserved_source_geometry")
    return actions


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


def _remove_checkpoint_restart_directives(lines: list[str]) -> List[str]:
    actions: List[str] = []
    if _remove_route_keywords(lines, {"MOREAD"}):
        actions.append("route_remove_moread")

    removed_moinp = False
    for idx in range(len(lines) - 1, -1, -1):
        if MOINP_RE.match(lines[idx]):
            del lines[idx]
            removed_moinp = True
    if removed_moinp:
        actions.append("moinp_removed")
    return actions


def _remove_route_keywords(lines: list[str], keywords: set[str]) -> bool:
    route_idx = find_route_idx(lines)
    if route_idx is None:
        return False
    stripped = lines[route_idx].strip()
    if not stripped.startswith("!"):
        return False

    tokens = stripped[1:].split()
    kept = [token for token in tokens if token.upper() not in keywords]
    if len(kept) == len(tokens):
        return False
    lines[route_idx] = "! " + " ".join(kept)
    return True


def _numbered_xyz_index_from_name(name: str) -> int | None:
    xyz_match = re.match(r"^.+\.(\d{3})\.xyz$", name, re.IGNORECASE)
    if xyz_match is None:
        return None
    return int(xyz_match.group(1))


def _xyzfile_name_from_geometry(lines: list[str]) -> str | None:
    for line in lines:
        match = GEOM_HEADER_RE.match(line.strip())
        if match is None or match.group(1).lower() != "xyzfile":
            continue
        raw_ref = (match.group(4) or "").strip().strip('"')
        return Path(raw_ref).name
    return None


def _numbered_xyz_index_from_geometry(lines: list[str]) -> int | None:
    name = _xyzfile_name_from_geometry(lines)
    if name is None:
        return None
    return _numbered_xyz_index_from_name(name)


def _cumulative_numbered_xyz_index_from_geometry(
    lines: list[str],
    *,
    source_inp: Path,
    seen: set[Path] | None = None,
) -> int | None:
    name = _xyzfile_name_from_geometry(lines)
    if name is None:
        return None
    index = _numbered_xyz_index_from_name(name)
    if index is None:
        return None

    stem_match = re.match(r"^(?P<stem>.+)\.\d{3}\.xyz$", name, re.IGNORECASE)
    if stem_match is None:
        return index
    parent_inp = source_inp.with_name(f"{stem_match.group('stem')}.inp")
    try:
        parent_resolved = parent_inp.resolve()
        source_resolved = source_inp.resolve()
    except OSError:
        return index
    if parent_resolved == source_resolved:
        return index

    seen_paths = set(seen or set())
    if parent_resolved in seen_paths:
        return index
    try:
        if not parent_inp.exists():
            return index
        parent_lines = parent_inp.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return index
    seen_paths.add(source_resolved)
    parent_index = _cumulative_numbered_xyz_index_from_geometry(
        parent_lines,
        source_inp=parent_inp,
        seen=seen_paths,
    )
    if parent_index is None:
        return index
    return parent_index + index


def _reverse_simple_scan_line(line: str) -> str | None:
    match = _SIMPLE_SCAN_COORD_LINE_RE.match(line)
    if match is None:
        return None
    return (
        f"{match.group('prefix')}{match.group('end')}"
        f"{match.group('sep1')}{match.group('start')}"
        f"{match.group('sep2')}{match.group('points')}{match.group('suffix')}"
    )


def _reverse_continuation_scan_line(
    line: str,
    original_line: str,
    *,
    completed_points: int,
) -> str | None:
    match = _SIMPLE_SCAN_COORD_LINE_RE.match(line)
    original_match = _SIMPLE_SCAN_COORD_LINE_RE.match(original_line)
    if match is None or original_match is None:
        return None
    source_points = int(match.group("points"))
    if completed_points <= 0 or source_points <= 0:
        return None
    total_points = completed_points + source_points
    return (
        f"{match.group('prefix')}{match.group('end')}"
        f"{match.group('sep1')}{original_match.group('start')}"
        f"{match.group('sep2')}{total_points}{match.group('suffix')}"
    )


def _reverse_simple_scan_path(
    lines: list[str],
    *,
    source_inp: Path,
    selected_inp: Path,
    target_inp: Path,
) -> List[str]:
    scan_line_indices = _simple_scan_coord_line_indices(lines)
    if not scan_line_indices:
        return []
    if not _scan_lines_share_total_points(lines, scan_line_indices):
        return []

    first_match = _SIMPLE_SCAN_COORD_LINE_RE.match(lines[scan_line_indices[0]])
    if first_match is None:
        return []
    source_points = int(first_match.group("points"))
    last_scan_xyz = highest_numbered_scan_xyz(source_inp)
    if last_scan_xyz is None:
        return []
    last_index, geometry_file = last_scan_xyz
    if last_index < source_points:
        return []

    completed_points = _cumulative_numbered_xyz_index_from_geometry(
        lines,
        source_inp=source_inp,
    )
    is_continuation = (
        source_inp.resolve() != selected_inp.resolve() and completed_points is not None
    )
    selected_lines: list[str] = []
    selected_scan_line_indices: list[int] = []
    if is_continuation:
        selected_lines = selected_inp.read_text(encoding="utf-8", errors="ignore").splitlines()
        selected_scan_line_indices = _simple_scan_coord_line_indices(selected_lines)
        if len(selected_scan_line_indices) != len(scan_line_indices):
            return []
        if not _scan_lines_share_total_points(selected_lines, selected_scan_line_indices):
            return []

    rewritten_lines: list[tuple[int, str]] = []
    total_points: int | None = None
    for offset, line_idx in enumerate(scan_line_indices):
        if is_continuation:
            assert completed_points is not None
            original_line = selected_lines[selected_scan_line_indices[offset]]
            rewritten = _reverse_continuation_scan_line(
                lines[line_idx],
                original_line,
                completed_points=completed_points,
            )
            source_match = _SIMPLE_SCAN_COORD_LINE_RE.match(lines[line_idx])
            total_points = (
                completed_points + int(source_match.group("points")) if source_match else None
            )
        else:
            rewritten = _reverse_simple_scan_line(lines[line_idx])
            source_match = _SIMPLE_SCAN_COORD_LINE_RE.match(lines[line_idx])
            total_points = int(source_match.group("points")) if source_match else None
        if rewritten is None or rewritten == lines[line_idx] or total_points is None:
            return []
        rewritten_lines.append((line_idx, rewritten))

    if not replace_geometry_with_xyzfile(lines, geometry_file, target_inp.parent):
        return []
    for line_idx, rewritten in rewritten_lines:
        lines[line_idx] = rewritten

    actions = [
        "scants_reverse_scan",
        f"scants_reverse_scan_points_{total_points}",
        f"geometry_restart_from_{geometry_file.name}",
    ]
    if is_continuation:
        actions.append(f"scants_reverse_scan_from_continuation_after_point_{completed_points:03d}")
    else:
        actions.append("scants_reverse_scan_from_forward_surface")
    return actions


def _scan_endpoint_extension_steps(
    total_points: int,
    *,
    min_extension_steps: int,
    extension_fraction: float | None,
) -> int:
    extension_steps = max(0, min_extension_steps)
    if extension_fraction is not None:
        extension_steps = max(extension_steps, round((total_points - 1) * extension_fraction))
    return extension_steps


def _continue_simple_scan_from_last_numbered_xyz(
    lines: list[str],
    *,
    source_inp: Path,
    target_inp: Path,
    min_extension_steps: int,
    extension_fraction: float | None,
) -> List[str]:
    last_scan_xyz = highest_numbered_scan_xyz(source_inp)
    if last_scan_xyz is None:
        return []
    last_index, geometry_file = last_scan_xyz

    scan_line_indices = _simple_scan_coord_line_indices(lines)
    if not scan_line_indices:
        return []
    if not _scan_lines_share_total_points(lines, scan_line_indices):
        return []

    first_match = _SIMPLE_SCAN_COORD_LINE_RE.match(lines[scan_line_indices[0]])
    if first_match is None:
        return []
    old_points = int(first_match.group("points"))
    extension_steps = _scan_endpoint_extension_steps(
        old_points,
        min_extension_steps=min_extension_steps,
        extension_fraction=extension_fraction,
    )
    new_points = old_points + extension_steps - last_index
    if extension_steps <= 0 or new_points <= 0:
        return []

    rewritten_lines: list[tuple[int, str]] = []
    for line_idx in scan_line_indices:
        rewritten = _continue_simple_scan_line(
            lines[line_idx],
            completed_points=last_index,
            extension_steps=extension_steps,
            new_points=new_points,
        )
        if rewritten is None or rewritten == lines[line_idx]:
            return []
        rewritten_lines.append((line_idx, rewritten))

    if not replace_geometry_with_xyzfile(lines, geometry_file, target_inp.parent):
        return []
    for line_idx, rewritten in rewritten_lines:
        lines[line_idx] = rewritten
    return [
        f"scants_scan_endpoint_extended_by_{extension_steps:03d}_step",
        f"scants_scan_range_continued_after_point_{last_index:03d}",
        f"geometry_restart_from_{geometry_file.name}",
    ]


def _complete_simple_scan_to_original_endpoint(
    lines: list[str],
    *,
    source_inp: Path,
    target_inp: Path,
) -> List[str]:
    last_scan_xyz = highest_numbered_scan_xyz(source_inp)
    if last_scan_xyz is None:
        return []
    last_index, geometry_file = last_scan_xyz

    scan_line_indices = _simple_scan_coord_line_indices(lines)
    if not scan_line_indices:
        return []
    if not _scan_lines_share_total_points(lines, scan_line_indices):
        return []

    first_match = _SIMPLE_SCAN_COORD_LINE_RE.match(lines[scan_line_indices[0]])
    if first_match is None:
        return []
    total_points = int(first_match.group("points"))
    new_points = total_points - last_index
    if new_points <= 0:
        return []

    rewritten_lines: list[tuple[int, str]] = []
    for line_idx in scan_line_indices:
        rewritten = _resume_simple_scan_line(lines[line_idx], completed_points=last_index)
        if rewritten is None or rewritten == lines[line_idx]:
            return []
        rewritten_lines.append((line_idx, rewritten))

    if not replace_geometry_with_xyzfile(lines, geometry_file, target_inp.parent):
        return []
    for line_idx, rewritten in rewritten_lines:
        lines[line_idx] = rewritten
    return [
        "scants_endpoint_scan_to_original_endpoint",
        f"scants_endpoint_scan_from_point_{last_index:03d}",
        f"scants_endpoint_scan_points_{new_points}",
        f"geometry_restart_from_{geometry_file.name}",
    ]


def _replace_scants_route_with_endpoint_opt(lines: list[str]) -> List[str]:
    route_idx = find_route_idx(lines)
    if route_idx is None:
        return []
    stripped = lines[route_idx].strip()
    if not stripped.startswith("!"):
        return []
    tokens = stripped[1:].split()
    if not any(token.upper() == "SCANTS" for token in tokens):
        return []

    rewritten: list[str] = []
    inserted_opt = False
    removed_post_scan_tokens = False
    for token in tokens:
        upper = token.upper()
        if upper == "SCANTS":
            if not inserted_opt and "OPT" not in {item.upper() for item in rewritten}:
                rewritten.append("Opt")
                inserted_opt = True
            continue
        if upper in {"FREQ", "NUMFREQ", "ANFREQ", "IRC"}:
            removed_post_scan_tokens = True
            continue
        rewritten.append(token)
    lines[route_idx] = "! " + " ".join(rewritten)

    actions = ["scants_endpoint_scan_route_to_opt"]
    if removed_post_scan_tokens:
        actions.append("scants_endpoint_scan_removed_freq_irc")
    return actions


def _first_scants_route_line(inp_path: Path) -> str:
    try:
        for line in inp_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("!") and SCANTS_ROUTE_RE.search(stripped):
                return stripped
    except OSError:
        return ""
    return ""


def _restore_selected_scants_route(lines: list[str], selected_inp: Path) -> List[str]:
    route = _first_scants_route_line(selected_inp)
    if not route:
        return []
    route_idx = find_route_idx(lines)
    if route_idx is None:
        return []
    if lines[route_idx].strip() == route:
        return []
    lines[route_idx] = route
    return ["scants_reverse_scan_route_restored"]


def _continue_simple_scan_line(
    line: str,
    *,
    completed_points: int,
    extension_steps: int,
    new_points: int,
) -> str | None:
    match = _SIMPLE_SCAN_COORD_LINE_RE.match(line)
    if match is None:
        return None
    total_points = int(match.group("points"))
    if completed_points <= 0 or extension_steps <= 0 or new_points <= 0 or total_points <= 1:
        return None

    start = float(match.group("start"))
    end = float(match.group("end"))
    step = (end - start) / (total_points - 1)
    if step == 0:
        return None
    next_start = start + step * completed_points
    extended_end = end + step * extension_steps
    return (
        f"{match.group('prefix')}{_format_scan_float(next_start)}"
        f"{match.group('sep1')}{_format_scan_float(extended_end)}"
        f"{match.group('sep2')}{new_points}{match.group('suffix')}"
    )


def _format_scan_float(value: float) -> str:
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    if text == "-0":
        return "0"
    return text


def prepare_scants_endpoint_scan_input(
    *,
    source_inp: Path,
    target_inp: Path,
    max_memory_gb: int | None = None,
) -> tuple[Path | None, List[str]]:
    if not input_uses_scants(source_inp):
        return None, []

    lines = source_inp.read_text(encoding="utf-8", errors="ignore").splitlines()
    actions = _remove_checkpoint_restart_directives(lines)
    route_actions = _replace_scants_route_with_endpoint_opt(lines)
    if not route_actions:
        return None, []
    scan_actions = _complete_simple_scan_to_original_endpoint(
        lines,
        source_inp=source_inp,
        target_inp=target_inp,
    )
    if not scan_actions:
        return None, []
    actions.extend(route_actions)
    actions.extend(scan_actions)

    if max_memory_gb is not None and clamp_maxcore_to_budget(lines, max_memory_gb=max_memory_gb):
        actions.append("maxcore_clamped_to_budget")

    target_inp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target_inp, actions


def prepare_scants_scan_retry_input(
    *,
    source_inp: Path,
    target_inp: Path,
    retry_number: int,
    max_memory_gb: int | None = None,
) -> tuple[Path | None, List[str]]:
    if not input_uses_scants(source_inp):
        return None, []

    lines = source_inp.read_text(encoding="utf-8", errors="ignore").splitlines()
    actions = apply_scants_failed_scan_retry_rewrite(
        lines,
        retry_number=retry_number,
        source_inp=source_inp,
        target_inp=target_inp,
    )
    if not actions:
        return None, []

    if max_memory_gb is not None and clamp_maxcore_to_budget(lines, max_memory_gb=max_memory_gb):
        actions.append("maxcore_clamped_to_budget")

    target_inp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target_inp, actions


def prepare_scants_reverse_scan_retry_input(
    *,
    source_inp: Path,
    selected_inp: Path,
    target_inp: Path,
    max_memory_gb: int | None = None,
) -> tuple[Path | None, List[str]]:
    if not input_uses_scants(selected_inp):
        return None, []

    lines = source_inp.read_text(encoding="utf-8", errors="ignore").splitlines()
    actions: List[str] = []
    if not input_uses_scants(source_inp):
        route_actions = _restore_selected_scants_route(lines, selected_inp)
        if not route_actions:
            return None, []
        actions.extend(route_actions)
    actions.extend(_remove_checkpoint_restart_directives(lines))
    reverse_actions = _reverse_simple_scan_path(
        lines,
        source_inp=source_inp,
        selected_inp=selected_inp,
        target_inp=target_inp,
    )
    if not reverse_actions:
        return None, []
    actions.extend(reverse_actions)

    if max_memory_gb is not None and clamp_maxcore_to_budget(lines, max_memory_gb=max_memory_gb):
        actions.append("maxcore_clamped_to_budget")

    target_inp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target_inp, actions


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
