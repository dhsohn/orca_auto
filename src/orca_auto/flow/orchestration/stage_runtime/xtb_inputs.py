from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from orca_auto.core.engine_process import atomic_write_confined_bytes, ensure_confined_directory
from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file
from orca_auto.core.utils.coercion import normalize_text, safe_int
from orca_auto.flow.xyz_utils import (
    load_output_xyz_frames,
    load_verified_xyz_frames,
    load_xyz_atom_sequence,
)


def _safe_xcontrol_target_name(value: Any, *, fallback_name: str) -> str:
    text = normalize_text(value) or fallback_name
    text = text.strip()
    if not text or text in {".", ".."}:
        raise ValueError("xcontrol target must be a plain file name")

    posix = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or ".." in posix.parts
        or ".." in windows.parts
    ):
        raise ValueError(f"xcontrol target must be a plain file name: {text!r}")
    if "/" in text or "\\" in text:
        raise ValueError(f"xcontrol target must be a plain file name: {text!r}")
    return text


def _safe_xcontrol_target_path(job_dir: Path, target_name: str) -> Path:
    job_dir_resolved = job_dir.expanduser().resolve()
    return job_dir_resolved / target_name


def _materialize_xtb_override_xcontrol(
    job_dir: Path,
    *,
    overrides: dict[str, Any],
    fallback_name: str = "workflow_xcontrol.inp",
) -> str:
    xcontrol_file = normalize_text(overrides.get("xcontrol_file"))
    xcontrol_text = normalize_text(overrides.get("xcontrol_text"))
    xcontrol_lines_value = overrides.get("xcontrol_lines")
    target_name = _safe_xcontrol_target_name(
        overrides.get("xcontrol"),
        fallback_name=fallback_name,
    )
    target_path = _safe_xcontrol_target_path(job_dir, target_name)

    if xcontrol_file:
        source = Path(xcontrol_file).expanduser().resolve()
        if source.exists() and source.is_file():
            atomic_write_confined_bytes(
                job_dir,
                target_path,
                read_stable_regular_file(source),
                label="xTB materialized xcontrol",
            )
            return target_name

    lines: list[str] = []
    if isinstance(xcontrol_lines_value, (list, tuple)):
        lines = [str(item) for item in xcontrol_lines_value]
    elif isinstance(xcontrol_lines_value, str) and xcontrol_lines_value.strip():
        lines = xcontrol_lines_value.splitlines()
    elif xcontrol_text:
        lines = xcontrol_text.splitlines()

    if lines:
        atomic_write_confined_bytes(
            job_dir,
            target_path,
            ("\n".join(lines) + "\n").encode("utf-8"),
            label="xTB materialized xcontrol",
        )
        return target_name

    return ""


def _stage_input_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _stage_input_rank(source: dict[str, Any]) -> int:
    return max(1, safe_int(source.get("rank", 1), default=1))


def _materialize_xtb_stage_input(source: dict[str, Any], target: Path) -> str:
    source_path = Path(normalize_text(source.get("artifact_path"))).expanduser().resolve()
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"xTB workflow input artifact not found: {source_path}")

    metadata = _stage_input_mapping(source.get("metadata"))
    frame_index = safe_int(metadata.get("source_frame_index", 0) or 0, default=0)

    output_identity = metadata.get("output_identity")
    frames = (
        load_verified_xyz_frames(source_path, output_identity)
        if isinstance(output_identity, dict)
        else load_output_xyz_frames(source_path)
    )
    if not frames:
        raise ValueError(f"xTB workflow input artifact is not a valid finite XYZ: {source_path}")
    if frame_index > 0:
        if frame_index > len(frames):
            raise ValueError(
                f"Requested CREST frame {frame_index} is unavailable in retained artifact: {source_path}"
            )
        selected_frame = frames[frame_index - 1]
    else:
        if len(frames) != 1:
            raise ValueError(
                f"xTB workflow input artifact must contain exactly one XYZ frame: {source_path}"
            )
        selected_frame = frames[0]

    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_confined_bytes(
        target.parent,
        target,
        selected_frame.render().encode("utf-8"),
        label="xTB materialized XYZ input",
    )
    return str(target.resolve())


def _materialize_xtb_path_inputs(
    payload: dict[str, Any],
    *,
    job_dir: Path,
) -> tuple[Path, Path]:
    reactants_dir = job_dir / "reactants"
    products_dir = job_dir / "products"
    ensure_confined_directory(job_dir, reactants_dir, label="xTB reactants directory")
    ensure_confined_directory(job_dir, products_dir, label="xTB products directory")

    reactant_source = _stage_input_mapping(payload.get("reactant_source"))
    product_source = _stage_input_mapping(payload.get("product_source"))
    reactant_target = reactants_dir / f"r{_stage_input_rank(reactant_source)}.xyz"
    product_target = products_dir / f"p{_stage_input_rank(product_source)}.xyz"
    _materialize_xtb_stage_input(reactant_source, reactant_target)
    _materialize_xtb_stage_input(product_source, product_target)
    reactant_atoms = tuple(atom.casefold() for atom in load_xyz_atom_sequence(reactant_target))
    product_atoms = tuple(atom.casefold() for atom in load_xyz_atom_sequence(product_target))
    if reactant_atoms != product_atoms:
        raise ValueError(
            "xTB path-search endpoints must have identical atom counts and element order"
        )
    return reactant_target, product_target
