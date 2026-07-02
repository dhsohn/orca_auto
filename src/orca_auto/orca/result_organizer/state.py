from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.paths import resolve_artifact_path

from ..state import load_state, save_state, write_report_files
from ..types import RunState
from .models import OrganizePlan


def _normalize_attempt_artifact_paths(
    attempts: Any,
    *,
    source_dir: Path,
    target_dir: Path,
) -> None:
    if not isinstance(attempts, list):
        return
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        inp_path = attempt.get("inp_path")
        if isinstance(inp_path, str):
            attempt["inp_path"] = _normalize_moved_artifact_path(
                inp_path,
                source_dir=source_dir,
                target_dir=target_dir,
            )
        out_path = attempt.get("out_path")
        if isinstance(out_path, str):
            attempt["out_path"] = _normalize_moved_artifact_path(
                out_path,
                source_dir=source_dir,
                target_dir=target_dir,
            )


def _normalize_final_result_artifact_path(
    final_result: Any,
    *,
    source_dir: Path,
    target_dir: Path,
) -> None:
    if not isinstance(final_result, dict):
        return
    last_out_path = final_result.get("last_out_path")
    if isinstance(last_out_path, str):
        final_result["last_out_path"] = _normalize_moved_artifact_path(
            last_out_path,
            source_dir=source_dir,
            target_dir=target_dir,
        )


def _sync_state_after_relocation(
    *,
    state_dir: Path,
    source_dir: Path,
    target_dir: Path,
) -> RunState:
    state = load_state(state_dir)
    if state is None:
        raise RuntimeError(f"Relocated directory has invalid state: {state_dir}")

    state["reaction_dir"] = str(target_dir)

    selected_inp = state.get("selected_inp")
    if isinstance(selected_inp, str):
        state["selected_inp"] = _normalize_moved_artifact_path(
            selected_inp,
            source_dir=source_dir,
            target_dir=target_dir,
        )

    _normalize_attempt_artifact_paths(
        state.get("attempts"),
        source_dir=source_dir,
        target_dir=target_dir,
    )
    _normalize_final_result_artifact_path(
        state.get("final_result"),
        source_dir=source_dir,
        target_dir=target_dir,
    )

    save_state(state_dir, state)
    write_report_files(state_dir, state)
    return state


def sync_state_after_move(plan: OrganizePlan) -> RunState:
    return _sync_state_after_relocation(
        state_dir=plan.target_abs_path,
        source_dir=plan.source_dir,
        target_dir=plan.target_abs_path,
    )


def sync_state_after_rollback(plan: OrganizePlan) -> RunState:
    return _sync_state_after_relocation(
        state_dir=plan.source_dir,
        source_dir=plan.target_abs_path,
        target_dir=plan.source_dir,
    )


def _remap_moved_path(path_text: str, source_dir: Path, target_dir: Path) -> str:
    path = Path(path_text)
    if not path.is_absolute():
        return path_text
    try:
        rel = path.relative_to(source_dir)
    except ValueError:
        return path_text
    return str(target_dir / rel)


def _normalize_moved_artifact_path(path_text: str, source_dir: Path, target_dir: Path) -> str:
    remapped = _remap_moved_path(path_text, source_dir, target_dir)
    resolved = resolve_artifact_path(remapped, target_dir)
    if resolved is None:
        return remapped
    return str(resolved)
