from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .input_blocks import orca_line_tokens


@dataclass(frozen=True)
class OrcaSelectedInputArtifacts:
    selected_inp: str
    selected_input_xyz: str

    @property
    def selected_input_path(self) -> str:
        return self.selected_input_xyz or self.selected_inp


def selected_input_artifacts(selected_inp: str | Path | None) -> OrcaSelectedInputArtifacts:
    selected_inp_text = _path_text(selected_inp)
    return OrcaSelectedInputArtifacts(
        selected_inp=selected_inp_text,
        selected_input_xyz=derive_selected_input_xyz(selected_inp_text),
    )


def derive_selected_input_xyz(selected_inp: str | Path | None) -> str:
    inp_path = _resolve_existing_path(selected_inp)
    if inp_path is None or inp_path.is_dir():
        return ""
    try:
        text = inp_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    for line in text.splitlines():
        xyz_ref = _xyzfile_reference(line)
        if xyz_ref:
            return _resolve_artifact_path(xyz_ref, inp_path.parent)
    return ""


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


def _xyzfile_reference(line: str) -> str:
    tokens = orca_line_tokens(line)
    if len(tokens) < 5:
        return ""
    if tokens[0].value != "*" or tokens[1].value.lower() != "xyzfile":
        return ""
    return tokens[4].value
