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
from orca_auto.core.utils import normalize_text as _normalize_text
from orca_auto.core.utils import now_utc_iso, timestamped_token
from orca_auto.core.utils import safe_int as _safe_int

MANIFEST_FILE_NAME = "xtb_job.yaml"
SUPPORTED_JOB_TYPES = {"path_search", "opt", "sp", "hess", "ranking"}
_EXCLUDE_RE = re.compile(r"(?:^xtb_|^struc|^coord)", re.IGNORECASE)


def _normalize_key(value: str) -> str:
    collapsed = re.sub(r"[^A-Za-z0-9._-]+", "_", _normalize_text(value)).strip("._-")
    return collapsed.lower() or "unknown_key"


def _as_int(value: Any, default: int) -> int:
    return _safe_int(value, default=default)


def load_job_manifest(job_dir: Path) -> dict[str, Any]:
    return _shared_run_dir.load_yaml_job_manifest(
        job_dir,
        MANIFEST_FILE_NAME,
        missing_message="Missing xTB job manifest: {path}",
        invalid_message="Invalid xTB job manifest: {path}",
    )


def job_type(manifest: dict[str, Any]) -> str:
    value = _normalize_text(manifest.get("job_type", "path_search")).lower() or "path_search"
    if value not in SUPPORTED_JOB_TYPES:
        raise ValueError(
            f"Unsupported xtb job_type: {value}. supported={sorted(SUPPORTED_JOB_TYPES)}"
        )
    return value


def _xyz_files(root: Path) -> list[Path]:
    files = [path.resolve() for path in root.glob("*.xyz") if path.is_file()]
    return sorted(files, key=lambda path: path.name.lower())


def _choose_xyz(root: Path, explicit_name: str, *, label: str) -> Path:
    files = _xyz_files(root)
    if explicit_name:
        candidate = (root / explicit_name).resolve()
        if not candidate.exists() or not candidate.is_file():
            raise ValueError(f"{label} file not found: {candidate}")
        if candidate.suffix.lower() != ".xyz":
            raise ValueError(f"{label} file must be .xyz: {candidate}")
        return candidate
    if not files:
        raise ValueError(f"No .xyz files found in {label} directory: {root}")
    preferred = [path for path in files if not _EXCLUDE_RE.search(path.name)]
    return preferred[0] if preferred else files[0]


def _choose_root_xyz(job_dir: Path, explicit_name: str) -> Path:
    return _choose_xyz(job_dir, explicit_name, label="input")


def _resolve_path_search_inputs(
    resolved_job_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    reaction_key = _normalize_key(
        _normalize_text(manifest.get("reaction_key")) or resolved_job_dir.name
    )

    reactants_dir = resolved_job_dir / "reactants"
    products_dir = resolved_job_dir / "products"
    if not reactants_dir.exists() or not reactants_dir.is_dir():
        raise ValueError(f"Missing reactants directory: {reactants_dir}")
    if not products_dir.exists() or not products_dir.is_dir():
        raise ValueError(f"Missing products directory: {products_dir}")

    reactant_xyz = _choose_xyz(
        reactants_dir,
        _normalize_text(manifest.get("reactant_xyz")),
        label="reactant",
    )
    product_xyz = _choose_xyz(
        products_dir,
        _normalize_text(manifest.get("product_xyz")),
        label="product",
    )

    input_summary = {
        "reactant_xyz": str(reactant_xyz),
        "product_xyz": str(product_xyz),
        "reactant_count": len(_xyz_files(reactants_dir)),
        "product_count": len(_xyz_files(products_dir)),
    }
    return {
        "job_type": "path_search",
        "reaction_key": reaction_key,
        "selected_input_xyz": reactant_xyz,
        "secondary_input_xyz": product_xyz,
        "input_summary": input_summary,
    }


def _resolve_ranking_inputs(
    resolved_job_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    candidates_dir_name = (
        _normalize_text(manifest.get("candidates_dir", "candidates")) or "candidates"
    )
    candidates_dir = (resolved_job_dir / candidates_dir_name).resolve()
    if not candidates_dir.exists() or not candidates_dir.is_dir():
        raise ValueError(f"Missing ranking candidates directory: {candidates_dir}")
    candidate_paths = _xyz_files(candidates_dir)
    if not candidate_paths:
        raise ValueError(f"No .xyz candidates found in ranking directory: {candidates_dir}")
    molecule_key = _normalize_key(
        _normalize_text(manifest.get("molecule_key"))
        or _normalize_text(manifest.get("reaction_key"))
        or resolved_job_dir.name
    )
    top_n = max(1, _as_int(manifest.get("top_n", 3), 3))
    input_summary = {
        "candidates_dir": str(candidates_dir),
        "candidate_count": len(candidate_paths),
        "candidate_paths": [str(path) for path in candidate_paths],
        "top_n": top_n,
    }
    return {
        "job_type": "ranking",
        "reaction_key": molecule_key,
        "selected_input_xyz": candidate_paths[0],
        "secondary_input_xyz": None,
        "input_summary": input_summary,
    }


def _resolve_single_input_job_inputs(
    resolved_job_dir: Path,
    manifest: dict[str, Any],
    *,
    resolved_type: str,
) -> dict[str, Any]:
    input_xyz = _choose_root_xyz(resolved_job_dir, _normalize_text(manifest.get("input_xyz")))
    molecule_key = _normalize_key(
        _normalize_text(manifest.get("molecule_key"))
        or _normalize_text(manifest.get("reaction_key"))
        or input_xyz.stem
        or resolved_job_dir.name
    )
    return {
        "job_type": resolved_type,
        "reaction_key": molecule_key,
        "selected_input_xyz": input_xyz,
        "secondary_input_xyz": None,
        "input_summary": {
            "input_xyz": str(input_xyz),
            "input_count": 1,
        },
    }


def resolve_job_inputs(job_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    resolved_job_dir = job_dir.expanduser().resolve()
    resolved_type = job_type(manifest)

    if resolved_type == "path_search":
        return _resolve_path_search_inputs(resolved_job_dir, manifest)
    if resolved_type == "ranking":
        return _resolve_ranking_inputs(resolved_job_dir, manifest)
    return _resolve_single_input_job_inputs(
        resolved_job_dir,
        manifest,
        resolved_type=resolved_type,
    )


def resolve_job_dir(cfg: AppConfig, raw_job_dir: str) -> Path:
    return _shared_run_dir.resolve_engine_job_dir(
        cfg,
        raw_job_dir,
        engine="xtb",
        workflow_error_message=(
            "Job directory must be under a workflow-local xTB root: "
            "<runs_root>/<workflow_id>/02_xtb/..."
        ),
        validate_job_dir_fn=validate_job_dir,
        workflow_paths_from_path_fn=workflow_workspace_internal_engine_paths_from_path,
    )


def new_job_id() -> str:
    return timestamped_token("xtb")


def queued_state_payload(
    *,
    job_id: str,
    job_dir: Path,
    selected_input_xyz: Path,
    job_type: str,
    reaction_key: str,
    input_summary: dict[str, Any],
    resource_request: dict[str, int] | None = None,
) -> dict[str, Any]:
    now = now_utc_iso()
    candidate_count = int(input_summary.get("candidate_count", 0) or 0)
    resources = dict(resource_request or {})
    return build_engine_artifact_payload(
        engine="xtb",
        job=EngineArtifactJob(
            id=job_id,
            queue_id="",
            dir=str(job_dir),
            app_name="orca_auto_xtb",
            task_id=job_id,
        ),
        status=EngineArtifactStatus(state="queued"),
        input=EngineArtifactInput(
            primary_path=str(selected_input_xyz),
            selected_xyz_path=str(selected_input_xyz),
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
            "job_type": job_type,
            "reaction_key": reaction_key,
            "input_summary": dict(input_summary),
            "candidate_count": candidate_count,
            "candidate_paths": list(input_summary.get("candidate_paths", [])),
            "selected_candidate_paths": [],
        },
    )
