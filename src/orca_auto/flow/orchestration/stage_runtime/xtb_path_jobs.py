from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

from orca_auto.core.artifacts import XTB_JOB_MANIFEST_FILE
from orca_auto.core.engine_process import atomic_write_confined_bytes, ensure_confined_directory
from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file
from orca_auto.core.utils import normalize_text, safe_int
from orca_auto.flow.orchestration.stage_runtime.shared import (
    _manifest_override_mapping,
)
from orca_auto.flow.orchestration.stage_runtime.xtb_retry import (
    _xtb_path_job_dir,
    xtb_retry_recipe_impl,
)
from orca_auto.flow.orchestration.stage_views import WorkflowStageView, WorkflowTaskView
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

    # A bare drive prefix ("C:escape.inp") carries no separator, so the
    # separator test alone cannot see it. Windows drive paths are a declared
    # non-goal (ROADMAP.md), so they stay rejected here.
    if "/" in text or "\\" in text or bool(PureWindowsPath(text).drive):
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


def _write_xtb_recipe_xcontrol(job_dir: Path, recipe: dict[str, Any]) -> str:
    xcontrol_name = normalize_text(recipe.get("xcontrol_name"))
    if xcontrol_name:
        atomic_write_confined_bytes(
            job_dir,
            job_dir / xcontrol_name,
            ("\n".join(str(line) for line in recipe.get("xcontrol_lines", ())) + "\n").encode(
                "utf-8"
            ),
            label="xTB path recipe xcontrol",
        )
    return xcontrol_name


def _base_xtb_path_manifest(
    task_view: WorkflowTaskView, overrides: dict[str, Any]
) -> dict[str, Any]:
    task_resource_request = task_view.resource_request()
    manifest_payload: dict[str, Any] = {
        "job_type": "path_search",
        "gfn": 2,
        "charge": 0,
        "uhf": 0,
    }
    reserved_keys = {
        "job_type",
        "reaction_key",
        "reactant_xyz",
        "product_xyz",
        "xcontrol",
        "xcontrol_file",
        "xcontrol_text",
        "xcontrol_lines",
    }
    for key, value in overrides.items():
        if key not in reserved_keys:
            manifest_payload[key] = value
    manifest_payload["resources"] = {
        "max_cores": safe_int(task_resource_request.get("max_cores"), default=8),
        "max_memory_gb": safe_int(task_resource_request.get("max_memory_gb"), default=32),
    }
    return manifest_payload


def _write_xtb_path_manifest(
    *,
    task_view: WorkflowTaskView,
    payload: dict[str, Any],
    recipe: dict[str, Any],
    job_dir: Path,
    reactant_target: Path,
    product_target: Path,
    stage_id: str,
) -> str:
    overrides = _manifest_override_mapping(payload.get("job_manifest_overrides"))
    manifest_payload = _base_xtb_path_manifest(task_view, overrides)
    xcontrol_name = _write_xtb_recipe_xcontrol(job_dir, recipe)
    xcontrol_override_name = (
        "" if xcontrol_name else _materialize_xtb_override_xcontrol(job_dir, overrides=overrides)
    )
    selected_xcontrol_name = xcontrol_name or xcontrol_override_name

    manifest_payload["reaction_key"] = normalize_text(payload.get("reaction_key")) or stage_id
    manifest_payload["reactant_xyz"] = reactant_target.name
    manifest_payload["product_xyz"] = product_target.name
    if selected_xcontrol_name:
        manifest_payload["xcontrol"] = selected_xcontrol_name

    atomic_write_confined_bytes(
        job_dir,
        job_dir / XTB_JOB_MANIFEST_FILE,
        yaml.safe_dump(manifest_payload, sort_keys=False, allow_unicode=False).encode("utf-8"),
        label="xTB path manifest",
    )
    return selected_xcontrol_name


def _record_xtb_path_job_payload(
    *,
    task_view: WorkflowTaskView,
    payload: dict[str, Any],
    recipe: dict[str, Any],
    job_dir: Path,
    reactant_target: Path,
    product_target: Path,
    attempt_number: int,
) -> None:
    task_view.record_xtb_path_job_payload(
        recipe=recipe,
        job_dir=job_dir,
        reactant_target=reactant_target,
        product_target=product_target,
        attempt_number=attempt_number,
        reaction_key=normalize_text(payload.get("reaction_key")),
        normalize_text=normalize_text,
    )


def _record_xtb_path_job_metadata(
    *,
    stage_view: WorkflowStageView,
    recipe: dict[str, Any],
    attempt_number: int,
) -> None:
    stage_view.record_xtb_path_job_metadata(
        recipe=recipe,
        attempt_number=attempt_number,
        normalize_text=normalize_text,
    )


def _record_xtb_path_attempt(
    *,
    stage_view: WorkflowStageView,
    payload: dict[str, Any],
    recipe: dict[str, Any],
    job_dir: Path,
    selected_xcontrol_name: str,
    attempt_number: int,
) -> None:
    stage_view.record_xtb_path_attempt(
        recipe=recipe,
        job_dir=job_dir,
        manifest_path=(job_dir / XTB_JOB_MANIFEST_FILE).resolve(),
        xcontrol_path=(job_dir / selected_xcontrol_name).resolve()
        if selected_xcontrol_name
        else "",
        reaction_key=normalize_text(payload.get("reaction_key")),
        attempt_number=attempt_number,
        normalize_text=normalize_text,
    )


def write_xtb_path_job_impl(
    stage: dict[str, Any],
    *,
    xtb_allowed_root: Path,
    workflow_id: str,
    attempt_number: int,
) -> str:
    del workflow_id
    stage_view = WorkflowStageView(stage)
    task_view = stage_view.task
    payload = task_view.payload()
    recipe = xtb_retry_recipe_impl(attempt_number)
    stage_id = stage_view.stage_id()
    job_dir = _xtb_path_job_dir(xtb_allowed_root, stage_id, attempt_number)
    ensure_confined_directory(xtb_allowed_root, job_dir, label="xTB path stage job directory")
    reactant_target, product_target = _materialize_xtb_path_inputs(payload, job_dir=job_dir)
    selected_xcontrol_name = _write_xtb_path_manifest(
        task_view=task_view,
        payload=payload,
        recipe=recipe,
        job_dir=job_dir,
        reactant_target=reactant_target,
        product_target=product_target,
        stage_id=stage_id,
    )
    _record_xtb_path_job_payload(
        task_view=task_view,
        payload=payload,
        recipe=recipe,
        job_dir=job_dir,
        reactant_target=reactant_target,
        product_target=product_target,
        attempt_number=attempt_number,
    )
    _record_xtb_path_job_metadata(
        stage_view=stage_view,
        recipe=recipe,
        attempt_number=attempt_number,
    )
    _record_xtb_path_attempt(
        stage_view=stage_view,
        payload=payload,
        recipe=recipe,
        job_dir=job_dir,
        selected_xcontrol_name=selected_xcontrol_name,
        attempt_number=attempt_number,
    )
    return str(job_dir)


def ensure_xtb_job_dir_impl(
    stage: dict[str, Any],
    *,
    xtb_allowed_root: Path,
    workflow_id: str,
) -> str:
    task_view = WorkflowStageView(stage).task
    payload = task_view.payload()
    existing = normalize_text(payload.get("job_dir"))
    if existing:
        return existing
    return write_xtb_path_job_impl(
        stage, xtb_allowed_root=xtb_allowed_root, workflow_id=workflow_id, attempt_number=0
    )


__all__ = [
    "ensure_xtb_job_dir_impl",
    "write_xtb_path_job_impl",
]
