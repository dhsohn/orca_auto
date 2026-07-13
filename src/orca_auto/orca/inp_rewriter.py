from __future__ import annotations

from pathlib import Path

from orca_auto.core.utils.persistence import atomic_write_text

from .input_blocks import (
    BLOCK_START_RE,
    GEOM_HEADER_RE,
    MOINP_RE,
    nonempty_file,
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
    clamp_maxcore_to_budget,
    ensure_submission_resource_request,
    maxcore_mb_per_core,
    read_resource_request_from_input,
)
from .retry_policy import RetryRecipeName
from .retry_recipes import (
    apply_retry_recipe as _apply_retry_recipe,
)
from .scants import (
    apply_scants_optts_resume_rewrite,
    apply_scants_relaxed_scan_resume_rewrite,
    input_uses_scants,
    prepare_scants_optts_fallback_input,
    prepare_scants_scan_retry_input,
)

__all__ = [
    "GEOM_HEADER_RE",
    "BLOCK_START_RE",
    "MOINP_RE",
    "ensure_submission_resource_request",
    "maxcore_mb_per_core",
    "prepare_checkpoint_restart_input",
    "prepare_scants_optts_fallback_input",
    "prepare_scants_scan_retry_input",
    "read_resource_request_from_input",
    "rewrite_for_retry",
]


def rewrite_for_retry(
    source_inp: Path,
    target_inp: Path,
    step: RetryRecipeName,
    *,
    max_memory_gb: int | None = None,
    allow_no_effective_change: bool = False,
) -> list[str]:
    lines = source_inp.read_text(encoding="utf-8", errors="ignore").splitlines()
    actions: list[str] = []

    actions.extend(_apply_retry_recipe(lines, step))

    if max_memory_gb is not None and clamp_maxcore_to_budget(lines, max_memory_gb=max_memory_gb):
        actions.append("maxcore_clamped_to_budget")

    if not allow_no_effective_change and not _has_effective_retry_change(actions):
        raise RuntimeError("no_retry_rewrite_available")

    atomic_write_text(target_inp, "\n".join(lines).rstrip() + "\n")
    return actions


def _has_effective_retry_change(actions: list[str]) -> bool:
    return any(
        action.startswith("checkpoint_restart_from_")
        or action.startswith("geometry_restart_from_")
        or action.startswith("geom_hessian")
        or action.startswith("route_add_tightscf")
        or action.startswith("scf_maxiter")
        or action.startswith("route_add_looseopt")
        or action.startswith("maxcore_increased")
        for action in actions
    )


def prepare_checkpoint_restart_input(
    source_inp: Path,
    target_inp: Path,
    reaction_dir: Path,
) -> tuple[Path | None, list[str]]:
    lines = source_inp.read_text(encoding="utf-8", errors="ignore").splitlines()
    actions: list[str] = []
    if not _apply_checkpoint_restart(lines, actions, source_inp, target_inp):
        return None, []

    # ORCA writes the same-stem XYZ during relaxed ScanTS scan steps too, so the
    # file alone cannot prove that ORCA has entered OPTTS refinement.  Promote a
    # resumed ScanTS input to OPTTS only when the previous output either contains
    # an explicit TS-refinement marker or a completed actual-energy scan surface
    # from which we can select the maximum numbered scan point.
    scants_resume_actions: list[str] = []
    uses_scants = input_uses_scants(source_inp)
    if uses_scants:
        scants_resume_actions = apply_scants_optts_resume_rewrite(
            lines=lines,
            source_inp=source_inp,
            target_inp=target_inp,
            out_path=source_inp.with_suffix(".out"),
        )
        actions.extend(scants_resume_actions)

    if not scants_resume_actions:
        if uses_scants:
            actions.extend(apply_scants_relaxed_scan_resume_rewrite(lines, source_inp))
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
    return candidate if nonempty_file(candidate) else None


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
    return max(candidates.values(), key=lambda p: p.stat().st_mtime_ns)
