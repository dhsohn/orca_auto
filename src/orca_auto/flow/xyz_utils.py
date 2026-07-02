from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from orca_auto.core.utils.coercion import safe_int

_ENERGY_PATTERNS = (
    re.compile(r"energy:\s*([-+]?\d+(?:\.\d+)?)", re.IGNORECASE),
    re.compile(r"\bE\s+([-+]?\d+(?:\.\d+)?)", re.IGNORECASE),
)


@dataclass(frozen=True)
class XYZFrame:
    index: int
    natoms: int
    comment: str
    atom_lines: tuple[str, ...]
    energy: float | None

    def render(self) -> str:
        lines = [str(self.natoms), self.comment, *self.atom_lines]
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class XYZParseResult:
    frames: tuple[XYZFrame, ...] = ()
    error_reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.frames) and not self.error_reason


@dataclass(frozen=True)
class _XYZFrameParseStep:
    frame: XYZFrame | None
    next_cursor: int
    error_reason: str = ""


def _parse_energy(comment: str) -> float | None:
    for pattern in _ENERGY_PATTERNS:
        match = pattern.search(comment)
        if not match:
            continue
        try:
            return float(match.group(1))
        except ValueError:
            continue
    return None


def _line_has_xyz_tokens(line: str) -> bool:
    tokens = line.split()
    if len(tokens) < 4:
        return False
    try:
        float(tokens[1])
        float(tokens[2])
        float(tokens[3])
    except ValueError:
        return False
    return True


def _xyz_parse_error(reason: str) -> XYZParseResult:
    return XYZParseResult(error_reason=reason)


def _resolve_xyz_path(path: str | Path) -> tuple[Path | None, str]:
    try:
        xyz_path = Path(path).expanduser().resolve()
    except OSError:
        return None, "path_error"
    if not xyz_path.exists() or not xyz_path.is_file():
        return None, "missing_or_not_file"
    return xyz_path, ""


def _read_xyz_lines(xyz_path: Path) -> tuple[list[str], str]:
    try:
        return xyz_path.read_text(encoding="utf-8", errors="ignore").splitlines(), ""
    except OSError:
        return [], "read_error"


def _skip_blank_lines(raw_lines: Sequence[str], cursor: int) -> int:
    while cursor < len(raw_lines) and not raw_lines[cursor].strip():
        cursor += 1
    return cursor


def _parse_xyz_frame(raw_lines: Sequence[str], cursor: int, index: int) -> _XYZFrameParseStep:
    try:
        natoms = int(raw_lines[cursor].strip())
    except ValueError:
        return _XYZFrameParseStep(None, cursor, "invalid_atom_count")
    if natoms <= 0:
        return _XYZFrameParseStep(None, cursor, "non_positive_atom_count")
    if cursor + 2 + natoms > len(raw_lines):
        return _XYZFrameParseStep(None, cursor, "truncated_frame")

    comment = raw_lines[cursor + 1]
    atom_lines = tuple(raw_lines[cursor + 2 : cursor + 2 + natoms])
    if len(atom_lines) != natoms or any(not _line_has_xyz_tokens(line) for line in atom_lines):
        return _XYZFrameParseStep(None, cursor, "invalid_atom_line")

    frame = XYZFrame(
        index=index,
        natoms=natoms,
        comment=comment,
        atom_lines=atom_lines,
        energy=_parse_energy(comment),
    )
    return _XYZFrameParseStep(frame, cursor + 2 + natoms)


def _parse_xyz_frames(raw_lines: Sequence[str]) -> XYZParseResult:
    frames: list[XYZFrame] = []
    cursor = 0
    while cursor < len(raw_lines):
        cursor = _skip_blank_lines(raw_lines, cursor)
        if cursor >= len(raw_lines):
            break
        step = _parse_xyz_frame(raw_lines, cursor, len(frames) + 1)
        if step.error_reason:
            return _xyz_parse_error(step.error_reason)
        if step.frame is not None:
            frames.append(step.frame)
        cursor = step.next_cursor
    if not frames:
        return _xyz_parse_error("empty_xyz")
    return XYZParseResult(frames=tuple(frames))


def parse_xyz_file(path: str | Path) -> XYZParseResult:
    xyz_path, error_reason = _resolve_xyz_path(path)
    if error_reason or xyz_path is None:
        return _xyz_parse_error(error_reason)

    raw_lines, error_reason = _read_xyz_lines(xyz_path)
    if error_reason:
        return _xyz_parse_error(error_reason)
    return _parse_xyz_frames(raw_lines)


def load_xyz_frames(path: str | Path) -> tuple[XYZFrame, ...]:
    return parse_xyz_file(path).frames


def has_xyz_geometry(path: str | Path) -> bool:
    return bool(load_xyz_frames(path))


def load_xyz_atom_sequence(path: str | Path) -> tuple[str, ...]:
    xyz_path = Path(path).expanduser().resolve()
    frames = load_xyz_frames(xyz_path)
    if not frames:
        raise ValueError(f"Invalid or empty XYZ file: {xyz_path}")
    if len(frames) != 1:
        raise ValueError(f"Expected a single-geometry XYZ file: {xyz_path}")
    return tuple(line.split()[0] for line in frames[0].atom_lines)


def _source_size_bytes(xyz_path: Path) -> int:
    if xyz_path.exists() and xyz_path.is_file():
        return int(xyz_path.stat().st_size)
    return 0


def _build_orca_geometry_metadata(
    xyz_path: Path,
    parse_result: XYZParseResult,
    candidate_kind: str,
) -> dict[str, object]:
    frames = parse_result.frames
    metadata: dict[str, object] = {
        "source_artifact_path": str(xyz_path),
        "frame_count": len(frames),
        "candidate_kind": str(candidate_kind).strip(),
        "source_size_bytes": _source_size_bytes(xyz_path),
    }
    if parse_result.error_reason:
        metadata["parse_error"] = parse_result.error_reason
    return metadata


def _add_selection_metadata(
    metadata: dict[str, object],
    frame: XYZFrame | None,
    selection_reason: str,
) -> None:
    if frame is None:
        metadata["selection_reason"] = selection_reason
        return
    metadata["selected_frame_index"] = frame.index
    metadata["selection_reason"] = selection_reason
    if frame.energy is not None:
        metadata["selected_frame_energy"] = frame.energy


def _select_energy_ranked_frame(frames: tuple[XYZFrame, ...]) -> tuple[XYZFrame, str]:
    energetic = [frame for frame in frames if frame.energy is not None]
    if energetic:
        return (
            max(
                energetic,
                key=lambda item: item.energy if item.energy is not None else float("-inf"),
            ),
            "highest_energy_frame",
        )
    return frames[len(frames) // 2], "middle_frame_fallback"


def _select_orca_frame(
    frames: tuple[XYZFrame, ...],
    candidate_kind: str,
    *,
    requested_frame_index: int = 0,
) -> tuple[XYZFrame | None, str]:
    if not frames:
        return None, "invalid_or_empty_xyz"
    if requested_frame_index > 0:
        if requested_frame_index > len(frames):
            return None, "requested_frame_unavailable"
        return frames[requested_frame_index - 1], "requested_frame"

    normalized_kind = str(candidate_kind).strip().lower()
    if normalized_kind == "ts_guess" and len(frames) != 1:
        return None, "ts_guess_requires_single_frame"
    if len(frames) == 1:
        return frames[0], "single_frame"

    if normalized_kind in {"ts_guess", "selected_path"}:
        return _select_energy_ranked_frame(frames)
    return frames[0], "first_frame"


def choose_orca_geometry_frame(
    path: str | Path, *, candidate_kind: str = "", source_frame_index: int = 0
) -> tuple[XYZFrame | None, dict[str, object]]:
    xyz_path = Path(path).expanduser().resolve()
    parse_result = parse_xyz_file(xyz_path)
    metadata = _build_orca_geometry_metadata(xyz_path, parse_result, candidate_kind)
    requested_frame_index = max(0, safe_int(source_frame_index, default=0))
    if requested_frame_index:
        metadata["requested_frame_index"] = requested_frame_index
    frame, selection_reason = _select_orca_frame(
        parse_result.frames,
        candidate_kind,
        requested_frame_index=requested_frame_index,
    )
    _add_selection_metadata(metadata, frame, selection_reason)
    return frame, metadata


def write_orca_ready_xyz(
    *,
    source_path: str | Path,
    target_path: str | Path,
    candidate_kind: str = "",
    source_frame_index: int = 0,
) -> dict[str, object]:
    frame, metadata = choose_orca_geometry_frame(
        source_path,
        candidate_kind=candidate_kind,
        source_frame_index=source_frame_index,
    )
    if frame is None:
        raise ValueError(f"No ORCA-ready XYZ geometry found in source candidate: {source_path}")
    target = Path(target_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(frame.render(), encoding="utf-8")
    metadata["materialized_xyz_path"] = str(target)
    return metadata


__all__ = [
    "XYZFrame",
    "XYZParseResult",
    "choose_orca_geometry_frame",
    "has_xyz_geometry",
    "load_xyz_frames",
    "load_xyz_atom_sequence",
    "parse_xyz_file",
    "write_orca_ready_xyz",
]
