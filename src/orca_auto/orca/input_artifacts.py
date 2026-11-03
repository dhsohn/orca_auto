"""Artifact paths derived from a job's selected ``.inp`` (its ``xyzfile`` sibling and stem)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .input_blocks import InputSource, find_geometry_block


@dataclass(frozen=True)
class OrcaSelectedInputArtifacts:
    selected_inp: str
    selected_input_xyz: str

    @property
    def selected_input_path(self) -> str:
        return self.selected_input_xyz or self.selected_inp


def xyzfile_input_path(lines: InputSource, inp_dir: Path) -> str:
    """The resolved ``* xyzfile`` geometry path an input's ``lines`` (or its parse) name, or ``""``."""
    block = find_geometry_block(lines)
    if block is None or not block.reference:
        return ""
    return _resolve_artifact_path(block.reference, inp_dir)


def _path_text(value: str | Path | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


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
