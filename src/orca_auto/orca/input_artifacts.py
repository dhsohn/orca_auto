"""Artifact paths derived from a job's selected ``.inp`` (its ``xyzfile`` sibling and stem)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .input_blocks import find_geometry_block


@dataclass(frozen=True)
class OrcaSelectedInputArtifacts:
    selected_inp: str
    selected_input_xyz: str

    @property
    def selected_input_path(self) -> str:
        return self.selected_input_xyz or self.selected_inp


def derive_selected_input_xyz(selected_inp: str | Path | None) -> str:
    inp_path = _resolve_existing_path(selected_inp)
    if inp_path is None or inp_path.is_dir():
        return ""
    try:
        text = inp_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    return xyzfile_input_path(text.splitlines(), inp_path.parent)


def xyzfile_input_path(lines: list[str], inp_dir: Path) -> str:
    """The resolved ``* xyzfile`` geometry path an input's ``lines`` name, or ``""``."""
    block = find_geometry_block(lines)
    if block is None or not block.reference:
        return ""
    return _resolve_artifact_path(block.reference, inp_dir)


def _path_text(value: str | Path | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _resolve_existing_path(value: Any) -> Path | None:
    text = _path_text(value)
    if not text:
        return None
    try:
        resolved = Path(text).expanduser().resolve()
    except OSError:
        return None
    if not resolved.exists():
        return None
    return resolved


def _resolve_artifact_path(path_value: str, base_dir: Path | None) -> str:
    text = path_value.strip()
    if not text:
        return ""
    candidate = Path(text).expanduser()
    if not candidate.is_absolute() and base_dir is not None:
        candidate = base_dir / candidate
    try:
        return str(candidate.resolve())
    except OSError:
        return str(candidate)
