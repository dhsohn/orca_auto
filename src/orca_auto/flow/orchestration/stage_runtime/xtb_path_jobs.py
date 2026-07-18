from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from orca_auto.core.engine_process import atomic_write_confined_bytes, ensure_confined_directory
from orca_auto.core.utils import normalize_text, safe_int
from orca_auto.flow.orchestration.stage_runtime.shared import (
    _manifest_override_mapping,
)
from orca_auto.flow.orchestration.stage_runtime.xtb_inputs import (
    _materialize_xtb_override_xcontrol,
    _materialize_xtb_path_inputs,
)
from orca_auto.flow.orchestration.stage_runtime.xtb_retry import (
    _xtb_path_job_dir,
    xtb_retry_recipe_impl,
)
from orca_auto.flow.orchestration.stage_views import WorkflowStageView, WorkflowTaskView


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
) -> tuple[str, str]:
    overrides = _manifest_override_mapping(payload.get("job_manifest_overrides"))
    manifest_payload = _base_xtb_path_manifest(task_view, overrides)
    namespace = normalize_text(recipe.get("namespace"))
    override_namespace = str(overrides.get("namespace", "")).strip()
    if namespace or override_namespace:
        raise ValueError("xTB namespace is not supported by the canonical artifact contract")
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
        job_dir / "xtb_job.yaml",
        yaml.safe_dump(manifest_payload, sort_keys=False, allow_unicode=False).encode("utf-8"),
        label="xTB path manifest",
    )
    return namespace, selected_xcontrol_name


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
    namespace: str,
    attempt_number: int,
) -> None:
    stage_view.record_xtb_path_attempt(
        recipe=recipe,
        job_dir=job_dir,
        manifest_path=(job_dir / "xtb_job.yaml").resolve(),
        xcontrol_path=(job_dir / selected_xcontrol_name).resolve()
        if selected_xcontrol_name
        else "",
        namespace=namespace,
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
    namespace, selected_xcontrol_name = _write_xtb_path_manifest(
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
        namespace=namespace,
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
