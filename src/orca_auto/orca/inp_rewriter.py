from __future__ import annotations

from pathlib import Path

from orca_auto.core.utils.persistence import atomic_write_text

from .input_blocks import (
    BLOCK_START_RE,
    GEOM_HEADER_RE,
    MOINP_RE,
    checkpoint_file_looks_intact,
)
from .input_blocks import (
    ensure_route_keywords as _ensure_route_keywords,
)
from .input_blocks import (
    replace_geometry_with_xyzfile as _replace_geometry_with_xyzfile,
)
from .input_blocks import (
    set_moinp as _set_moinp,
)
from .resource_directives import (
    ensure_submission_resource_request,
    maxcore_mb_per_core,
    prepare_submission_resource_request,
    read_resource_request_from_input,
)

__all__ = [
    "GEOM_HEADER_RE",
    "BLOCK_START_RE",
    "MOINP_RE",
    "ensure_submission_resource_request",
    "maxcore_mb_per_core",
    "prepare_submission_resource_request",
    "prepare_checkpoint_restart_input",
    "read_resource_request_from_input",
]


def prepare_checkpoint_restart_input(
    source_inp: Path,
    target_inp: Path,
    reaction_dir: Path,
) -> tuple[Path | None, list[str]]:
    lines = source_inp.read_text(encoding="utf-8", errors="ignore").splitlines()
    actions: list[str] = []
    if not _apply_checkpoint_restart(lines, actions, source_inp, target_inp):
        return None, []

    _apply_geometry_restart(lines, actions, source_inp, target_inp, reaction_dir)
    atomic_write_text(target_inp, "\n".join(lines).rstrip() + "\n")
    return target_inp, actions


def _apply_checkpoint_restart(
    lines: list[str],
    actions: list[str],
    source_inp: Path,
    target_inp: Path,
) -> bool:
    checkpoint = _matching_checkpoint_gbw(source_inp)
    if checkpoint is None:
        return False
    if checkpoint.resolve() == target_inp.with_suffix(".gbw").resolve():
        actions.append(f"checkpoint_restart_skipped_same_basename:{checkpoint.name}")
        return False

    actions.append(f"checkpoint_restart_from_{checkpoint.name}")
    if _ensure_route_keywords(lines, ["MORead"]):
        actions.append("route_add_moread")
    if _set_moinp(lines, checkpoint, target_inp.parent):
        actions.append("moinp_set")
    return True


def _matching_checkpoint_gbw(source_inp: Path) -> Path | None:
    candidate = source_inp.with_suffix(".gbw")
    return candidate if checkpoint_file_looks_intact(candidate) else None


def _apply_geometry_restart(
    lines: list[str],
    actions: list[str],
    source_inp: Path,
    target_inp: Path,
    reaction_dir: Path,
) -> None:
    geometry_file = _previous_attempt_xyz(source_inp)
    if geometry_file is None:
        actions.append("no_previous_xyz_file_found")
        geometry_file = _latest_geometry_file(reaction_dir)

    if geometry_file is None:
        actions.append("no_geometry_file_found")
    else:
        if _replace_geometry_with_xyzfile(lines, geometry_file, target_inp.parent):
            actions.append(f"geometry_restart_from_{geometry_file.name}")
        else:
            actions.append("geometry_restart_not_applied")


def _previous_attempt_xyz(source_inp: Path) -> Path | None:
    candidate = source_inp.with_suffix(".xyz")
    if candidate.exists():
        return candidate
    return None


def _latest_geometry_file(reaction_dir: Path) -> Path | None:
    candidates = {p.resolve(): p for p in reaction_dir.glob("*.xyz")}
    if not candidates:
        return None
    # Two geometries written in the same nanosecond would otherwise be ordered
    # by readdir, so the restart input would depend on the filesystem.
    return max(candidates.values(), key=lambda p: (p.stat().st_mtime_ns, p.name.lower()))


def resume_checkpoint_input_path(current_inp: Path) -> Path:
    return current_inp.with_name(f"{current_inp.stem}.resume.inp")
