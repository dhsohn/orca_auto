from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from orca_auto.core.commands import run_dir as _shared_run_dir
from orca_auto.core.config.engines import WorkflowEngineAppConfig as AppConfig
from orca_auto.core.engines.artifacts import (
    EngineArtifactInput,
    EngineArtifactJob,
    EngineArtifactResources,
    EngineArtifactStatus,
    EngineArtifactTimestamps,
    build_engine_artifact_payload,
)
from orca_auto.core.paths import validate_job_dir
from orca_auto.core.paths.workflow import workflow_workspace_internal_engine_paths_from_path
from orca_auto.core.utils import now_utc_iso, timestamped_token

_PREFERRED_EXCLUDE_RE = re.compile(r"(?:^crest_|^struc|^coord)", re.IGNORECASE)
MANIFEST_FILE_NAME = "crest_job.yaml"


def load_job_manifest(job_dir: Path) -> dict[str, Any]:
    return _shared_run_dir.load_yaml_job_manifest(
        job_dir,
        MANIFEST_FILE_NAME,
        invalid_message="Invalid CREST job manifest: {path}",
    )


def job_mode(manifest: dict[str, Any]) -> str:
    mode = str(manifest.get("mode", "standard")).strip().lower()
    if mode not in {"standard", "nci"}:
        raise ValueError("CREST mode must be 'standard' or 'nci'.")
    return mode


def select_latest_xyz(job_dir: Path) -> Path:
    resolved_job_dir = job_dir.expanduser().resolve()
    candidates = []
    for path in job_dir.glob("*.xyz"):
        resolved = path.resolve()
        if not resolved.is_relative_to(resolved_job_dir):
            raise ValueError(f"CREST input must stay inside the job directory: {path}")
        if resolved.is_file():
            candidates.append(resolved)
    if not candidates:
        raise ValueError(f"No .xyz file found in: {job_dir}")

    preferred = [path for path in candidates if not _PREFERRED_EXCLUDE_RE.search(path.name)]
    if preferred:
        candidates = preferred

    candidates.sort(key=lambda path: (path.stat().st_mtime_ns, path.name.lower()), reverse=True)
    return candidates[0]


def select_input_xyz(job_dir: Path, manifest: dict[str, Any]) -> Path:
    input_xyz = str(manifest.get("input_xyz", "")).strip()
    if input_xyz:
        candidate = (job_dir / input_xyz).resolve()
        if not candidate.is_relative_to(job_dir.expanduser().resolve()):
            raise ValueError(f"Manifest input_xyz must stay inside the job directory: {candidate}")
        if not candidate.exists() or not candidate.is_file():
            raise ValueError(f"Manifest input_xyz not found: {candidate}")
        if candidate.suffix.lower() != ".xyz":
            raise ValueError(f"Manifest input_xyz must point to a .xyz file: {candidate}")
        return candidate
    return select_latest_xyz(job_dir)


def resolve_job_dir(cfg: AppConfig, raw_job_dir: str) -> Path:
    return _shared_run_dir.resolve_engine_job_dir(
        cfg,
        raw_job_dir,
        engine="crest",
        workflow_error_message=(
            "Job directory must be under a workflow-local CREST root: "
            "<runs_root>/<workflow_id>/01_crest/..."
        ),
        validate_job_dir_fn=validate_job_dir,
        workflow_paths_from_path_fn=workflow_workspace_internal_engine_paths_from_path,
    )


def new_job_id() -> str:
    return timestamped_token("crest")


def queued_state_payload(
    *,
    job_id: str,
    job_dir: Path,
    selected_xyz: Path,
    mode: str,
    molecule_key: str = "",
    resource_request: dict[str, int] | None = None,
) -> dict[str, Any]:
    now = now_utc_iso()
    resources = dict(resource_request or {})
    return build_engine_artifact_payload(
        engine="crest",
        job=EngineArtifactJob(
            id=job_id,
            queue_id="",
            dir=str(job_dir),
            app_name="orca_auto_crest",
            task_id=job_id,
        ),
        status=EngineArtifactStatus(state="queued"),
        input=EngineArtifactInput(
            primary_path=str(selected_xyz),
            selected_xyz_path=str(selected_xyz),
        ),
        resources=EngineArtifactResources(
            request=resources,
            actual=dict(resources),
        ),
        timestamps=EngineArtifactTimestamps(
            created_at=now,
            updated_at=now,
        ),
        engine_payload={
            "molecule_key": molecule_key,
            "mode": mode,
        },
    )
