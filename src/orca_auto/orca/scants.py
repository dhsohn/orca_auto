from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from orca_auto.core.utils.persistence import atomic_write_text

from .input_blocks import (
    MOINP_RE,
    active_orca_directive_text,
    active_orca_line_text,
    file_route_lines,
    find_block_range,
    nonempty_file,
    orca_route_line,
    replace_geometry_with_xyzfile,
    route_line_indices,
)
from .parser import KCAL_PER_HARTREE
from .resource_directives import clamp_maxcore_to_budget

SCANTS_ROUTE_RE = re.compile(r"\bSCANTS\b", re.IGNORECASE)
# Interior scan-profile maxima below this prominence are endpoint/vdW noise, not
# a barrier a reverse ScanTS could locate.
SCANTS_BARRIER_NOISE_KCAL = 0.5
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


@dataclass(frozen=True)
class ScanCoordinateSpec:
    kind: str
    atoms: tuple[int, ...]
    start: float
    end: float
    points: int

    def label(self) -> str:
        atom_text = ",".join(str(atom) for atom in self.atoms)
        return f"{self.kind}({atom_text})"


def input_uses_scants(inp_path: Path) -> bool:
    """True when any route line requests ScanTS.

    Scans every route line (matching the whole-file scan retry_policy uses to
    classify inputs) so the retry policy and the ScanTS rewriters can never
    disagree about whether an input is a ScanTS job.
    """
    return bool(_first_scants_route_line(inp_path))


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


def scan_profile_interior_barrier_kcal(energies: Sequence[float]) -> float | None:
    """Prominence in kcal/mol of the highest interior maximum along a scan profile.

    Prominence is the smaller of the climbs from the lowest energy on either
    side of a point, so a profile that is monotonic up to noise scores ~0 while
    a genuine barrier scores its height above the shallower flank. ``None``
    when fewer than three points exist (no interior point to judge).
    """
    if len(energies) < 3:
        return None
    suffix_min = list(energies)
    for idx in range(len(suffix_min) - 2, -1, -1):
        suffix_min[idx] = min(suffix_min[idx], suffix_min[idx + 1])
    best = 0.0
    prefix_min = energies[0]
    for idx in range(1, len(energies) - 1):
        prominence = min(energies[idx] - prefix_min, energies[idx] - suffix_min[idx + 1])
        best = max(best, prominence)
        prefix_min = min(prefix_min, energies[idx])
    return best * KCAL_PER_HARTREE


def _peak_prominence_kcal(energies: Sequence[float], peak: int) -> float:
    """Topographic prominence of a local maximum: the smaller of the climbs
    from the lowest valley separating it from higher ground (or the profile
    edge) on each side."""
    left_min = energies[peak]
    idx = peak - 1
    while idx >= 0 and energies[idx] <= energies[peak]:
        left_min = min(left_min, energies[idx])
        idx -= 1
    right_min = energies[peak]
    idx = peak + 1
    while idx < len(energies) and energies[idx] <= energies[peak]:
        right_min = min(right_min, energies[idx])
        idx += 1
    return min(energies[peak] - left_min, energies[peak] - right_min) * KCAL_PER_HARTREE


def scan_profile_interior_maxima(
    energies: Sequence[float],
    *,
    threshold_kcal: float = SCANTS_BARRIER_NOISE_KCAL,
) -> list[tuple[int, float]]:
    """All interior local maxima with prominence above the noise threshold.

    Returns ``(list index, prominence in kcal/mol)`` pairs sorted by
    descending prominence — every barrier candidate a TS search should try,
    not just the global maximum (which may be a profile endpoint). Plateaus
    count once, at their first point.
    """
    candidates: list[tuple[int, float]] = []
    for idx in range(1, len(energies) - 1):
        if energies[idx] <= energies[idx - 1] or energies[idx] < energies[idx + 1]:
            continue
        prominence = _peak_prominence_kcal(energies, idx)
        if prominence >= threshold_kcal:
            candidates.append((idx, prominence))
    candidates.sort(key=lambda item: (-item[1], item[0]))
    return candidates


def parse_scan_coordinate(text: str) -> ScanCoordinateSpec | None:
    """Spec from a bare scan-coordinate string like ``B 0 1 = 1.20, 3.00, 10``."""
    match = _SIMPLE_SCAN_COORD_LINE_RE.match(text.strip())
    if match is None:
        return None
    prefix_tokens = match.group("prefix").split("=")[0].split()
    if len(prefix_tokens) < 2:
        return None
    try:
        atoms = tuple(int(token) for token in prefix_tokens[1:])
    except ValueError:
        return None
    return ScanCoordinateSpec(
        kind=prefix_tokens[0].upper(),
        atoms=atoms,
        start=float(match.group("start")),
        end=float(match.group("end")),
        points=int(match.group("points")),
    )


def format_scan_coordinate(spec: ScanCoordinateSpec) -> str:
    """The canonical scan-coordinate string ``parse_scan_coordinate`` accepts."""
    atoms = " ".join(str(atom) for atom in spec.atoms)
    return (
        f"{spec.kind} {atoms} = {_format_scan_float(spec.start)}, "
        f"{_format_scan_float(spec.end)}, {spec.points}"
    )


def first_scan_coordinate_spec(inp_path: Path) -> ScanCoordinateSpec | None:
    """Kind, atom indices, and range of the first simple scan coordinate line."""
    try:
        lines = inp_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    for idx in _simple_scan_coord_line_indices(lines):
        spec = parse_scan_coordinate(active_orca_line_text(lines[idx]))
        if spec is not None:
            return spec
    return None


def scants_guess_xyz_for_output(source_inp: Path, out_path: Path) -> Path | None:
    point = highest_scants_surface_point(out_path)
    if point is None:
        return None
    candidate = source_inp.with_name(f"{source_inp.stem}.{point.index:03d}.xyz")
    return candidate if nonempty_file(candidate) else None


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
    return candidate if nonempty_file(candidate) else None


def scants_optts_resume_guess_xyz_for_output(source_inp: Path, out_path: Path) -> Path | None:
    if output_indicates_scants_optts_refinement(out_path):
        refinement_xyz = _same_stem_xyz(source_inp)
        if refinement_xyz is not None:
            return refinement_xyz
    return scants_guess_xyz_for_output(source_inp, out_path)


def highest_numbered_scan_xyz(source_inp: Path) -> tuple[int, Path] | None:
    pattern = re.compile(rf"^{re.escape(source_inp.stem)}\.(\d{{3}})\.xyz$")
    best: tuple[int, Path] | None = None
    for candidate in source_inp.parent.glob("*.xyz"):
        match = pattern.match(candidate.name)
        if match is None or not nonempty_file(candidate):
            continue
        index = int(match.group(1))
        if best is None or index > best[0]:
            best = (index, candidate)
    return best


def highest_numbered_scan_xyz_index(source_inp: Path) -> int | None:
    last_scan_xyz = highest_numbered_scan_xyz(source_inp)
    return None if last_scan_xyz is None else last_scan_xyz[0]


def apply_scants_relaxed_scan_resume_rewrite(lines: list[str], source_inp: Path) -> list[str]:
    last_index = highest_numbered_scan_xyz_index(source_inp)
    if last_index is None:
        return []

    shared = _scan_lines_with_shared_total(lines)
    if shared is None:
        return []
    scan_line_indices, _total_points = shared

    rewrites = _scan_line_rewrites(
        lines,
        scan_line_indices,
        lambda line, _offset: _resume_simple_scan_line(line, completed_points=last_index),
    )
    if rewrites is None:
        return []
    _apply_line_rewrites(lines, rewrites)
    return [f"scants_scan_range_resumed_after_point_{last_index:03d}"]


def apply_scants_failed_scan_retry_rewrite(
    lines: list[str],
    *,
    retry_number: int,
    source_inp: Path | None = None,
    target_inp: Path | None = None,
) -> list[str]:
    """Rewrite failed ScanTS retries without checkpoint/last-geometry restarts.

    ScanTS failures can leave the same-stem ``*.xyz`` and ``*.gbw`` in a chemically
    bad state. Failed-attempt retries therefore preserve the input geometry from
    the source input and change the ScanTS recipe itself instead of adding MORead
    or replacing the geometry with the latest attempt artifact.
    """
    cleanup_actions = _remove_checkpoint_restart_directives(lines)
    recipe_actions: list[str] = []

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
        if not _GEOM_SCAN_START_RE.match(active_orca_line_text(lines[i])):
            i += 1
            continue
        scan_end = _scan_subblock_end(lines, i + 1, end)
        for idx in range(i + 1, scan_end - 1):
            if _SIMPLE_SCAN_COORD_LINE_RE.match(active_orca_line_text(lines[idx])):
                indices.append(idx)
        i = scan_end
    return indices


def _scan_lines_with_shared_total(lines: list[str]) -> tuple[list[int], int] | None:
    """Return simple-scan coordinate line indices with their shared point total.

    Yields ``None`` unless the geometry block holds at least one simple-scan
    coordinate line and every such line declares the same total point count.
    """
    scan_line_indices = _simple_scan_coord_line_indices(lines)
    if not scan_line_indices:
        return None
    totals: set[int] = set()
    for idx in scan_line_indices:
        match = _SIMPLE_SCAN_COORD_LINE_RE.match(active_orca_line_text(lines[idx]))
        if match is None:
            return None
        totals.add(int(match.group("points")))
    if len(totals) != 1:
        return None
    return scan_line_indices, totals.pop()


def _scan_line_rewrites(
    lines: list[str],
    indices: list[int],
    rewriter: Callable[[str, int], str | None],
) -> list[tuple[int, str]] | None:
    """Build an all-or-nothing rewrite plan for the given scan lines.

    Returns ``None`` when any line fails to rewrite (or rewrites to itself) so
    callers never apply a partial scan mutation.
    """
    rewrites: list[tuple[int, str]] = []
    for offset, line_idx in enumerate(indices):
        active_line = active_orca_line_text(lines[line_idx])
        rewritten = rewriter(active_line, offset)
        if rewritten is None or rewritten == active_line:
            return None
        rewrites.append((line_idx, rewritten))
    return rewrites


def _apply_line_rewrites(lines: list[str], rewrites: list[tuple[int, str]]) -> None:
    for line_idx, rewritten in rewrites:
        lines[line_idx] = rewritten


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


def _remove_checkpoint_restart_directives(lines: list[str]) -> list[str]:
    actions: list[str] = []
    if _remove_route_keywords(lines, {"MOREAD"}):
        actions.append("route_remove_moread")

    removed_moinp = False
    for idx in range(len(lines) - 1, -1, -1):
        if MOINP_RE.match(active_orca_directive_text(lines[idx])):
            del lines[idx]
            removed_moinp = True
    if removed_moinp:
        actions.append("moinp_removed")
    return actions


def _remove_route_keywords(lines: list[str], keywords: set[str]) -> bool:
    changed = False
    for route_idx in route_line_indices(lines):
        route = orca_route_line(lines[route_idx])
        if route is None:
            continue

        tokens = route[1:].split()
        kept = [token for token in tokens if token.upper() not in keywords]
        if len(kept) == len(tokens):
            continue
        lines[route_idx] = "! " + " ".join(kept)
        changed = True
    return changed


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
) -> list[str]:
    last_scan_xyz = highest_numbered_scan_xyz(source_inp)
    if last_scan_xyz is None:
        return []
    last_index, geometry_file = last_scan_xyz

    shared = _scan_lines_with_shared_total(lines)
    if shared is None:
        return []
    scan_line_indices, old_points = shared
    extension_steps = _scan_endpoint_extension_steps(
        old_points,
        min_extension_steps=min_extension_steps,
        extension_fraction=extension_fraction,
    )
    new_points = old_points + extension_steps - last_index
    if extension_steps <= 0 or new_points <= 0:
        return []

    rewrites = _scan_line_rewrites(
        lines,
        scan_line_indices,
        lambda line, _offset: _continue_simple_scan_line(
            line,
            completed_points=last_index,
            extension_steps=extension_steps,
            new_points=new_points,
        ),
    )
    if rewrites is None:
        return []
    if not replace_geometry_with_xyzfile(lines, geometry_file, target_inp.parent):
        return []
    _apply_line_rewrites(lines, rewrites)
    return [
        f"scants_scan_endpoint_extended_by_{extension_steps:03d}_step",
        f"scants_scan_range_continued_after_point_{last_index:03d}",
        f"geometry_restart_from_{geometry_file.name}",
    ]


def _scants_route_idx(lines: list[str]) -> int | None:
    for idx in route_line_indices(lines):
        route = orca_route_line(lines[idx])
        if route is None:
            continue
        tokens = route[1:].split()
        if any(token.upper() == "SCANTS" for token in tokens):
            return idx
    return None


def _first_scants_route_line(inp_path: Path) -> str:
    for line in file_route_lines(inp_path):
        if SCANTS_ROUTE_RE.search(line):
            return line
    return ""


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


def _write_prepared_input(
    lines: list[str],
    *,
    target_inp: Path,
    actions: list[str],
    max_memory_gb: int | None,
) -> tuple[Path, list[str]]:
    """Shared tail of the ``prepare_scants_*`` builders: clamp maxcore and write."""
    if max_memory_gb is not None and clamp_maxcore_to_budget(lines, max_memory_gb=max_memory_gb):
        actions.append("maxcore_clamped_to_budget")
    atomic_write_text(target_inp, "\n".join(lines).rstrip() + "\n")
    return target_inp, actions


def prepare_scants_scan_retry_input(
    *,
    source_inp: Path,
    target_inp: Path,
    retry_number: int,
    max_memory_gb: int | None = None,
) -> tuple[Path | None, list[str]]:
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
    return _write_prepared_input(
        lines, target_inp=target_inp, actions=actions, max_memory_gb=max_memory_gb
    )


def prepare_scants_optts_fallback_input(
    *,
    source_inp: Path,
    target_inp: Path,
    reaction_dir: Path,
    out_path: Path,
    max_memory_gb: int | None = None,
) -> tuple[Path | None, list[str]]:
    if not input_uses_scants(source_inp):
        return None, []

    guess_xyz = scants_guess_xyz_for_output(source_inp, out_path)
    if guess_xyz is None:
        return None, []

    lines = source_inp.read_text(encoding="utf-8", errors="ignore").splitlines()
    actions: list[str] = []

    if _replace_scants_route_with_optts(lines):
        actions.append("scants_fallback_to_optts")
    if _remove_geom_scan_subblock(lines):
        actions.append("scants_scan_block_removed")
    if not replace_geometry_with_xyzfile(lines, guess_xyz, target_inp.parent):
        return None, []
    actions.append(f"scants_guess_from_{guess_xyz.name}")
    return _write_prepared_input(
        lines, target_inp=target_inp, actions=actions, max_memory_gb=max_memory_gb
    )


def apply_scants_optts_resume_rewrite(
    *,
    lines: list[str],
    source_inp: Path,
    target_inp: Path,
    out_path: Path,
) -> list[str]:
    guess_xyz = scants_optts_resume_guess_xyz_for_output(source_inp, out_path)
    if guess_xyz is None:
        return []

    # All-or-nothing: mutate a working copy and publish only a complete
    # rewrite. Bailing after the route/scan-block edits would hand the caller
    # a half-converted input (OptTS without the geometry restart) that it then
    # writes under a plain checkpoint-restart action log.
    rewritten = list(lines)
    actions: list[str] = []
    if _replace_scants_route_with_optts(rewritten):
        actions.append("scants_resume_to_optts")
    if _remove_geom_scan_subblock(rewritten):
        actions.append("scants_scan_block_removed")
    if not replace_geometry_with_xyzfile(rewritten, guess_xyz, target_inp.parent):
        return []
    actions.append(f"geometry_restart_from_{guess_xyz.name}")
    lines[:] = rewritten
    return actions


def _replace_scants_route_with_optts(lines: list[str]) -> bool:
    route_idx = _scants_route_idx(lines)
    if route_idx is None:
        return False
    route = orca_route_line(lines[route_idx])
    if route is None:
        return False
    tokens = route[1:].split()
    rewritten: list[str] = []
    inserted_optts = False
    for token in tokens:
        if token.upper() == "SCANTS":
            if not inserted_optts:
                rewritten.append("OPTTS")
                inserted_optts = True
            continue
        rewritten.append(token)
    lines[route_idx] = "! " + " ".join(rewritten)
    return True


def _remove_geom_scan_subblock(lines: list[str]) -> bool:
    block = find_block_range(lines, "geom")
    if block is None:
        return False
    start, end, _needs_close = block
    changed = False
    i = start + 1
    while i < end:
        if not _GEOM_SCAN_START_RE.match(active_orca_line_text(lines[i])):
            i += 1
            continue
        remove_until = _scan_subblock_end(lines, i + 1, end)
        del lines[i:remove_until]
        end -= remove_until - i
        changed = True
    return changed


def _scan_subblock_end(lines: list[str], start: int, stop: int) -> int:
    for idx in range(start, stop):
        if _GEOM_END_RE.match(active_orca_line_text(lines[idx])):
            return idx + 1
    return stop
