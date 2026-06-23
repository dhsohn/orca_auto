from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from orca_auto.core.utils.coercion import normalize_text, safe_int
from orca_auto.flow.xyz_utils import load_xyz_frames


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
    target = (job_dir_resolved / target_name).resolve()
    try:
        target.relative_to(job_dir_resolved)
    except ValueError as exc:
        raise ValueError(f"xcontrol target escapes job_dir: {target}") from exc
    return target


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
            shutil.copy2(source, target_path)
            return target_name

    lines: list[str] = []
    if isinstance(xcontrol_lines_value, (list, tuple)):
        lines = [str(item) for item in xcontrol_lines_value]
    elif isinstance(xcontrol_lines_value, str) and xcontrol_lines_value.strip():
        lines = xcontrol_lines_value.splitlines()
    elif xcontrol_text:
        lines = xcontrol_text.splitlines()

    if lines:
        target_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
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

    target.parent.mkdir(parents=True, exist_ok=True)
    if frame_index > 0:
        frames = load_xyz_frames(source_path)
        if frame_index > len(frames):
            raise ValueError(
                f"Requested CREST frame {frame_index} is unavailable in retained artifact: {source_path}"
            )
        target.write_text(frames[frame_index - 1].render(), encoding="utf-8")
        return str(target.resolve())

    shutil.copy2(source_path, target)
    return str(target.resolve())


def _materialize_xtb_path_inputs(
    payload: dict[str, Any],
    *,
    job_dir: Path,
) -> tuple[Path, Path]:
    reactants_dir = job_dir / "reactants"
    products_dir = job_dir / "products"
    reactants_dir.mkdir(parents=True, exist_ok=True)
    products_dir.mkdir(parents=True, exist_ok=True)

    reactant_source = _stage_input_mapping(payload.get("reactant_source"))
    product_source = _stage_input_mapping(payload.get("product_source"))
    reactant_target = reactants_dir / f"r{_stage_input_rank(reactant_source)}.xyz"
    product_target = products_dir / f"p{_stage_input_rank(product_source)}.xyz"
    _materialize_xtb_stage_input(reactant_source, reactant_target)
    _materialize_xtb_stage_input(product_source, product_target)
    return reactant_target, product_target
