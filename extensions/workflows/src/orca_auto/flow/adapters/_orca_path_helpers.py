from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.paths import (
    first_existing_named_file,
    iter_existing_dirs,
    recent_file_candidates,
    resolved_path_text,
)
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.orca.input_artifacts import derive_selected_input_xyz


def resolve_candidate_path_impl(
    path_text: Any,
    *,
    path_factory: Callable[[str], Any] | None = None,
) -> Path | None:
    raw = normalize_text(path_text)
    if not raw:
        return None
    factory = path_factory or Path
    try:
        candidate = factory(raw).expanduser()
    except OSError:
        return None
    try:
        return candidate.resolve()
    except OSError:
        return None


def direct_dir_target_impl(
    target: str,
    *,
    path_factory: Callable[[str], Any] | None = None,
) -> Path | None:
    raw = normalize_text(target)
    if not raw:
        return None
    factory = path_factory or Path
    try:
        candidate = factory(raw).expanduser().resolve()
    except OSError:
        return None
    if not candidate.exists():
        return None
    return candidate.parent if candidate.is_file() else candidate


def resolve_artifact_path_impl(
    path_value: Any,
    base_dir: Path | None,
    *,
    path_factory: Callable[[str], Any] | None = None,
) -> str:
    raw = normalize_text(path_value)
    if not raw:
        return ""
    factory = path_factory or Path
    try:
        candidate = factory(raw).expanduser()
    except OSError:
        return raw
    if candidate.is_absolute():
        try:
            return str(candidate.resolve())
        except OSError:
            return str(candidate)
    if base_dir is None:
        return raw
    try:
        return str((base_dir / candidate).resolve())
    except OSError:
        return str(base_dir / candidate)


def derive_selected_input_xyz_impl(selected_inp: str) -> str:
    return derive_selected_input_xyz(selected_inp)


def iter_existing_dirs_impl(*candidates: Path | None) -> list[Path]:
    return iter_existing_dirs(*candidates)


def _resolved_path_text(path: Path) -> str:
    return resolved_path_text(path)


def _path_or_parent(path: Path | None) -> Path | None:
    if path is None:
        return None
    return path if path.is_dir() else path.parent


def _parent_if_present(path: Path | None) -> Path | None:
    return path.parent if path is not None else None


def _preferred_xyz_names(*paths: Path | None) -> list[str]:
    return [f"{path.stem}.xyz" for path in paths if path is not None]


def _first_existing_named_file(search_dirs: list[Path], filenames: list[str]) -> str:
    return first_existing_named_file(search_dirs, filenames)


def _recent_xyz_candidates(search_dirs: list[Path], source_input: Path | None) -> list[Path]:
    return recent_file_candidates(search_dirs, suffix=".xyz", exclude=source_input)


def prefer_orca_optimized_xyz_impl(
    *,
    selected_inp: str,
    selected_input_xyz: str,
    current_dir: Path | None,
    latest_known_path: str,
    last_out_path: str,
) -> str:
    selected_inp_path = resolve_candidate_path_impl(selected_inp)
    selected_input_xyz_path = resolve_candidate_path_impl(selected_input_xyz)
    last_out = resolve_candidate_path_impl(last_out_path)

    search_dirs = iter_existing_dirs_impl(
        _parent_if_present(selected_inp_path),
        current_dir,
        _path_or_parent(resolve_candidate_path_impl(latest_known_path)),
        _parent_if_present(last_out),
    )
    preferred_match = _first_existing_named_file(
        search_dirs, _preferred_xyz_names(selected_inp_path, last_out)
    )
    if preferred_match:
        return preferred_match

    source_input = None
    if selected_input_xyz_path is not None:
        try:
            source_input = selected_input_xyz_path.resolve()
        except OSError:
            source_input = selected_input_xyz_path

    xyz_candidates = _recent_xyz_candidates(search_dirs, source_input)
    if not xyz_candidates:
        return ""
    return _resolved_path_text(xyz_candidates[0])


__all__ = [
    "derive_selected_input_xyz_impl",
    "direct_dir_target_impl",
    "iter_existing_dirs_impl",
    "prefer_orca_optimized_xyz_impl",
    "resolve_artifact_path_impl",
    "resolve_candidate_path_impl",
]
