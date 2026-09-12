from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from orca_auto.core.engine_process import atomic_write_confined_bytes
from orca_auto.core.geometry_limits import MAX_ADMISSION_ATOMS, MAX_HESSIAN_ADMISSION_ATOMS
from orca_auto.core.queue.engine.input_snapshot import (
    MAX_INPUT_SNAPSHOT_BYTES,
    read_stable_regular_file,
)
from orca_auto.core.utils.coercion import safe_int

FINITE_NUMBER_PATTERN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
_NUMBER_END = r"(?![A-Za-z0-9_.])"
_ENERGY_PATTERNS = (
    # xTB: "energy: -34.1 gnorm: 0.001 xtb: 6.7.1"
    re.compile(rf"energy:\s*({FINITE_NUMBER_PATTERN}){_NUMBER_END}", re.IGNORECASE),
    # ORCA: "Coordinates from ORCA-job input E -100.5"
    re.compile(rf"\bE\s+({FINITE_NUMBER_PATTERN}){_NUMBER_END}", re.IGNORECASE),
    # CREST `crest_conformers.xyz` / `crest_best.xyz`: the comment line is the
    # bare total energy in Eh. A decimal point is required so a bare integer
    # label on a user-supplied frame is not read as an energy.
    re.compile(r"^\s*([-+]?\d+\.\d+(?:[Ee][-+]?\d+)?)\s*$"),
    # CREST `crest_rotamers.xyz`: energy, then a weight, then `!`.
    re.compile(
        r"^\s*([-+]?\d+\.\d+(?:[Ee][-+]?\d+)?)\s+[-+]?\d+(?:\.\d*)?(?:[Ee][-+]?\d+)?\s*!\s*$"
    ),
)
MAX_OUTPUT_XYZ_MATERIALIZATION_BYTES = 512 * 1024 * 1024
_ELEMENT_SYMBOLS = (
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn "
    "Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La "
    "Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po "
    "At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn "
    "Nh Fl Mc Lv Ts Og"
).split()
_ATOMIC_NUMBERS = {symbol.casefold(): index for index, symbol in enumerate(_ELEMENT_SYMBOLS, 1)}


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
            value = float(match.group(1))
        except ValueError:
            continue
        return value if math.isfinite(value) else None
    return None


def _line_has_xyz_tokens(line: str) -> bool:
    tokens = line.split()
    if len(tokens) < 4:
        return False
    try:
        coordinates = (float(tokens[1]), float(tokens[2]), float(tokens[3]))
    except ValueError:
        return False
    return all(math.isfinite(value) for value in coordinates)


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


def _read_xyz_payload(xyz_path: Path, *, max_bytes: int) -> tuple[bytes, str]:
    try:
        payload = read_stable_regular_file(
            xyz_path,
            max_bytes=max_bytes,
        )
        return payload, ""
    except (OSError, ValueError):
        return b"", "read_error"


def _first_declared_atom_count(payload: bytes) -> tuple[int | None, str]:
    cursor = 0
    while cursor < len(payload):
        newline = payload.find(b"\n", cursor)
        end = len(payload) if newline < 0 else newline
        raw_line = payload[cursor:end].rstrip(b"\r").strip()
        if raw_line:
            if len(raw_line) > 32:
                return None, "invalid_atom_count"
            try:
                return int(raw_line.decode("ascii", errors="strict")), ""
            except ValueError:
                return None, "invalid_atom_count"
        if newline < 0:
            break
        cursor = newline + 1
    return None, "empty_xyz"


def _parse_xyz_payload(payload: bytes, *, max_atoms: int) -> XYZParseResult:
    declared_atoms, error_reason = _first_declared_atom_count(payload)
    if error_reason:
        return _xyz_parse_error(error_reason)
    if declared_atoms is not None and declared_atoms > max_atoms:
        return _xyz_parse_error("atom_count_exceeds_limit")
    return _parse_xyz_frames(
        payload.decode("utf-8", errors="ignore").splitlines(),
        max_atoms=max_atoms,
    )


def _skip_blank_lines(raw_lines: Sequence[str], cursor: int) -> int:
    while cursor < len(raw_lines) and not raw_lines[cursor].strip():
        cursor += 1
    return cursor


def _parse_xyz_frame(
    raw_lines: Sequence[str],
    cursor: int,
    index: int,
    *,
    max_atoms: int,
) -> _XYZFrameParseStep:
    try:
        natoms = int(raw_lines[cursor].strip())
    except ValueError:
        return _XYZFrameParseStep(None, cursor, "invalid_atom_count")
    if natoms <= 0:
        return _XYZFrameParseStep(None, cursor, "non_positive_atom_count")
    if natoms > max_atoms:
        return _XYZFrameParseStep(None, cursor, "atom_count_exceeds_limit")
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


def _parse_xyz_frames(
    raw_lines: Sequence[str],
    *,
    max_atoms: int = MAX_ADMISSION_ATOMS,
) -> XYZParseResult:
    frames: list[XYZFrame] = []
    cursor = 0
    while cursor < len(raw_lines):
        cursor = _skip_blank_lines(raw_lines, cursor)
        if cursor >= len(raw_lines):
            break
        step = _parse_xyz_frame(
            raw_lines,
            cursor,
            len(frames) + 1,
            max_atoms=max_atoms,
        )
        if step.error_reason:
            return _xyz_parse_error(step.error_reason)
        if step.frame is not None:
            frames.append(step.frame)
        cursor = step.next_cursor
    if not frames:
        return _xyz_parse_error("empty_xyz")
    return XYZParseResult(frames=tuple(frames))


def parse_xyz_file(
    path: str | Path,
    *,
    max_bytes: int = MAX_INPUT_SNAPSHOT_BYTES,
    max_atoms: int = MAX_ADMISSION_ATOMS,
) -> XYZParseResult:
    if max_atoms < 1:
        raise ValueError("XYZ atom-count limit must be positive")
    xyz_path, error_reason = _resolve_xyz_path(path)
    if error_reason or xyz_path is None:
        return _xyz_parse_error(error_reason)

    payload, error_reason = _read_xyz_payload(xyz_path, max_bytes=max_bytes)
    if error_reason:
        return _xyz_parse_error(error_reason)
    return _parse_xyz_payload(payload, max_atoms=max_atoms)


def load_xyz_frames(
    path: str | Path,
    *,
    max_bytes: int = MAX_INPUT_SNAPSHOT_BYTES,
    max_atoms: int = MAX_ADMISSION_ATOMS,
) -> tuple[XYZFrame, ...]:
    return parse_xyz_file(path, max_bytes=max_bytes, max_atoms=max_atoms).frames


def load_output_xyz_frames(path: str | Path) -> tuple[XYZFrame, ...]:
    return load_xyz_frames(path, max_bytes=MAX_OUTPUT_XYZ_MATERIALIZATION_BYTES)


def load_verified_xyz_frames(
    path: str | Path,
    identity: Mapping[str, object],
) -> tuple[XYZFrame, ...]:
    source = Path(path).expanduser().resolve()
    if str(identity.get("path") or "") != str(source):
        raise ValueError("XYZ content identity names another artifact")
    payload = read_stable_regular_file(
        source,
        max_bytes=MAX_OUTPUT_XYZ_MATERIALIZATION_BYTES,
        require_single_link=True,
    )
    if (
        identity.get("size_bytes") != len(payload)
        or identity.get("sha256") != hashlib.sha256(payload).hexdigest()
    ):
        raise ValueError("XYZ artifact no longer matches its terminal content identity")
    return _parse_xyz_payload(payload, max_atoms=MAX_ADMISSION_ATOMS).frames


def validated_xyz_atom_count(
    path: str | Path,
    *,
    max_atoms: int = MAX_ADMISSION_ATOMS,
) -> int:
    parse_result = parse_xyz_file(path, max_atoms=max_atoms)
    if parse_result.error_reason == "atom_count_exceeds_limit":
        raise ValueError(f"XYZ molecule exceeds the server atom-count limit of {max_atoms}")
    if len(parse_result.frames) != 1 or parse_result.error_reason:
        raise ValueError("XYZ molecule must contain exactly one valid finite frame")
    return parse_result.frames[0].natoms


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


# Radon closes the last fully parameterized row for the supported engines.
MAX_SUPPORTED_ATOMIC_NUMBER = 86


def validate_electronic_state(
    path: str | Path,
    *,
    charge: int,
    uhf: int,
) -> dict[str, int]:
    frames = load_xyz_frames(path)
    if len(frames) != 1:
        raise ValueError("Electronic-state validation requires one finite XYZ frame")
    nuclear_charge = 0
    for line in frames[0].atom_lines:
        symbol = line.split()[0].casefold()
        atomic_number = _ATOMIC_NUMBERS.get(symbol)
        if atomic_number is None:
            raise ValueError(f"Unknown element symbol in XYZ input: {line.split()[0]!r}")
        if atomic_number > MAX_SUPPORTED_ATOMIC_NUMBER:
            raise ValueError(
                f"Element {line.split()[0]!r} exceeds the supported GFN atomic-number "
                f"range 1..{MAX_SUPPORTED_ATOMIC_NUMBER}"
            )
        nuclear_charge += atomic_number
    electron_count = nuclear_charge - charge
    if electron_count < 0:
        raise ValueError("Molecular charge leaves a negative electron count")
    if uhf < 0 or uhf > electron_count:
        raise ValueError("UHF unpaired-electron count must be between 0 and the electron count")
    if (electron_count - uhf) % 2 != 0:
        raise ValueError("Electron count and UHF unpaired-electron parity are inconsistent")
    return {
        "nuclear_charge": nuclear_charge,
        "electron_count": electron_count,
        "charge": charge,
        "uhf": uhf,
    }


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
    parse_result = parse_xyz_file(
        xyz_path,
        max_bytes=MAX_OUTPUT_XYZ_MATERIALIZATION_BYTES,
    )
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


def write_fragment_xyz(
    *,
    coordinates: Sequence[tuple[str, float, float, float]],
    atom_indices: Sequence[int],
    target_path: str | Path,
    comment: str = "",
) -> None:
    """Write a single-frame XYZ holding a fragment sliced from a full geometry.

    ``atom_indices`` are 0-based positions into ``coordinates`` (the complex's
    optimized Cartesian coordinates). Indices are validated fail-closed: an
    out-of-range or duplicate index, or an empty selection, raises ``ValueError``
    rather than emitting a malformed geometry.
    """
    if not isinstance(comment, str) or (comment and not comment.isprintable()):
        raise ValueError("fragment XYZ comment must be a single line")
    natoms = len(coordinates)
    selected = list(atom_indices)
    if not selected:
        raise ValueError("fragment atom_indices is empty")
    seen: set[int] = set()
    for index in selected:
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError(f"fragment atom index {index!r} is not an integer")
        if index < 0 or index >= natoms:
            raise ValueError(f"fragment atom index {index} is outside 0..{natoms - 1}")
        if index in seen:
            raise ValueError(f"fragment atom index {index} is duplicated")
        seen.add(index)
    lines = [str(len(selected)), comment]
    for index in selected:
        element, x, y, z = coordinates[index]
        numeric = (float(x), float(y), float(z))
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError(f"fragment atom {index} has non-finite coordinates")
        lines.append(
            f"{str(element):<2} {numeric[0]:18.10f} {numeric[1]:18.10f} {numeric[2]:18.10f}"
        )
    target = Path(target_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_confined_bytes(
        target.parent,
        target,
        ("\n".join(lines) + "\n").encode("utf-8"),
        label="materialized XYZ fragment",
    )


def write_orca_ready_xyz(
    *,
    source_path: str | Path,
    target_path: str | Path,
    candidate_kind: str = "",
    source_frame_index: int = 0,
    source_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if source_identity is None:
        frame, metadata = choose_orca_geometry_frame(
            source_path,
            candidate_kind=candidate_kind,
            source_frame_index=source_frame_index,
        )
    else:
        source = Path(source_path).expanduser().resolve()
        if str(source_identity.get("path") or "") != str(source):
            raise ValueError("ORCA source geometry identity names another artifact")
        payload = read_stable_regular_file(
            source,
            max_bytes=MAX_OUTPUT_XYZ_MATERIALIZATION_BYTES,
            require_single_link=True,
        )
        if (
            source_identity.get("size_bytes") != len(payload)
            or source_identity.get("sha256") != hashlib.sha256(payload).hexdigest()
        ):
            raise ValueError("ORCA source geometry no longer matches its terminal identity")
        parse_result = _parse_xyz_payload(payload, max_atoms=MAX_ADMISSION_ATOMS)
        metadata = _build_orca_geometry_metadata(source, parse_result, candidate_kind)
        requested_frame_index = max(0, safe_int(source_frame_index, default=0))
        if requested_frame_index:
            metadata["requested_frame_index"] = requested_frame_index
        frame, selection_reason = _select_orca_frame(
            parse_result.frames,
            candidate_kind,
            requested_frame_index=requested_frame_index,
        )
        _add_selection_metadata(metadata, frame, selection_reason)
        metadata["source_sha256"] = str(source_identity.get("sha256") or "")
    if frame is None:
        raise ValueError(f"No ORCA-ready XYZ geometry found in source candidate: {source_path}")
    target = Path(target_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_confined_bytes(
        target.parent,
        target,
        frame.render().encode("utf-8"),
        label="ORCA-ready XYZ",
    )
    metadata["materialized_xyz_path"] = str(target.resolve())
    return metadata


__all__ = [
    "MAX_ADMISSION_ATOMS",
    "MAX_HESSIAN_ADMISSION_ATOMS",
    "XYZFrame",
    "XYZParseResult",
    "choose_orca_geometry_frame",
    "has_xyz_geometry",
    "load_xyz_frames",
    "load_verified_xyz_frames",
    "load_output_xyz_frames",
    "load_xyz_atom_sequence",
    "validate_electronic_state",
    "validated_xyz_atom_count",
    "parse_xyz_file",
    "write_fragment_xyz",
    "write_orca_ready_xyz",
]
