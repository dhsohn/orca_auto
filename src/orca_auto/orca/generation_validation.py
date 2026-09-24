"""Shared path and directory-identity checks for ORCA generation artifacts.

Callers retain their identity decoding, owner-token, and content-hash policies.
"""

from __future__ import annotations

from pathlib import Path

from orca_auto.core.confined_io import require_confined_regular_file
from orca_auto.core.queue.generation import is_visible_generation_name


def require_bound_generation_directory(
    job_dir: Path,
    raw_generation_dir: Path,
    expected_identity: tuple[int, int],
) -> Path:
    generation_dir = raw_generation_dir.resolve(strict=True)
    details = raw_generation_dir.lstat()
    if (
        not raw_generation_dir.is_absolute()
        or raw_generation_dir != generation_dir
        or raw_generation_dir.is_symlink()
        or generation_dir.parent != job_dir
        or not is_visible_generation_name(generation_dir.name)
        or not generation_dir.is_dir()
        or (int(details.st_dev), int(details.st_ino)) != expected_identity
    ):
        raise ValueError("ORCA generation directory does not match its bound path and identity")
    return generation_dir


def require_generation_selected_input(
    generation_dir: Path,
    raw_selected: Path,
    *,
    label: str,
) -> Path:
    selected = require_confined_regular_file(generation_dir, raw_selected, label=label)
    if raw_selected != selected or selected.parent != generation_dir:
        raise ValueError("ORCA selected input is not a canonical direct generation file")
    return selected
