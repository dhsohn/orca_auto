from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.indexing import engines as _engine_locations
from orca_auto.core.indexing.engine_job_locations import (
    build_store_backed_engine_job_location_exports,
)
from orca_auto.core.paths.workflow import (
    iter_workflow_runtime_workspaces,
    workflow_stage_dirnames_for_engine,
    workflow_workspace_internal_engine_paths,
)

from .state import load_state


def job_type_identifier(job_type: str) -> str:
    normalized = _engine_locations.normalize_text(job_type).lower() or "unknown"
    return f"xtb_{normalized}"


def normalize_key(value: str) -> str:
    return _engine_locations.normalize_identifier(value, default="unknown_key")


def reaction_key_from_job_dir(job_dir: Path) -> str:
    return normalize_key(job_dir.name)


_LOCATION_EXPORTS = build_store_backed_engine_job_location_exports(
    engine="xtb",
    spec=_engine_locations.EngineLocationSpec(
        app_name="orca_auto_xtb",
        job_type_from_payload=job_type_identifier,
        default_molecule_key=lambda original_run_dir, _selected: reaction_key_from_job_dir(
            original_run_dir
        ),
        payload_kind_key="job_type",
        payload_kind_default="path_search",
        molecule_key_name="reaction_key",
    ),
    load_state_fn=load_state,
    load_report_json_fn=None,
    payload_kind_kwarg="job_type",
    molecule_key_kwarg="reaction_key",
    default_payload_kind_kwarg="default_job_type",
)

index_root_for_cfg = _LOCATION_EXPORTS.index_root_for_cfg
index_root_for_path = _LOCATION_EXPORTS.index_root_for_path
list_job_records_for_cfg = _LOCATION_EXPORTS.list_job_records_for_cfg
resolve_job_location_for_cfg = _LOCATION_EXPORTS.resolve_job_location_for_cfg
build_job_location_record = _LOCATION_EXPORTS.build_job_location_record
upsert_job_record = _LOCATION_EXPORTS.upsert_job_record
resolve_latest_job_dir = _LOCATION_EXPORTS.resolve_latest_job_dir


def record_from_artifacts(*, job_dir: Path, state: dict[str, Any], **kwargs: Any) -> Any:
    return _LOCATION_EXPORTS.record_from_artifacts(
        job_dir=job_dir,
        state=state,
        report=None,
        **kwargs,
    )


def runtime_roots_for_cfg(cfg: object) -> tuple[Path, ...]:
    workflow_root = _engine_locations.normalize_text(getattr(cfg, "workflow_root", ""))
    if not workflow_root:
        return _LOCATION_EXPORTS.runtime_roots_for_cfg(cfg)

    roots: list[Path] = []
    for workspace_dir in iter_workflow_runtime_workspaces(workflow_root, engine="xtb"):
        for stage_dirname in workflow_stage_dirnames_for_engine("xtb"):
            runtime_paths = workflow_workspace_internal_engine_paths(
                workspace_dir,
                engine="xtb",
                stage_dirname=stage_dirname,
            )
            root = runtime_paths["allowed_root"].expanduser().resolve()
            if root not in roots:
                roots.append(root)

    if roots:
        return tuple(roots)
    return _LOCATION_EXPORTS.runtime_roots_for_cfg(cfg)
