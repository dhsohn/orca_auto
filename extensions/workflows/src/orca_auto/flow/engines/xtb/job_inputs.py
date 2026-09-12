from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import XTB_JOB_MANIFEST_FILE
from orca_auto.core.commands import run_dir as _shared_run_dir
from orca_auto.core.config.engines import WorkflowEngineAppConfig as AppConfig
from orca_auto.core.engine_catalog import get_engine_catalog_entry
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
from orca_auto.flow.xyz_utils import load_xyz_frames

SUPPORTED_JOB_TYPES = frozenset(
    task_kind.removeprefix("xtb_") for task_kind in get_engine_catalog_entry("xtb").task_kinds
)
_EXCLUDE_RE = re.compile(r"(?:^xtb_|^struc|^coord)", re.IGNORECASE)
MAX_RANKING_CANDIDATES = 1000
DEFAULT_MAX_RANKING_EVALUATIONS = 100


def _normalize_key(value: str) -> str:
    collapsed = re.sub(r"[^A-Za-z0-9._-]+", "_", _normalize_text(value)).strip("._-")
    return collapsed.lower() or "unknown_key"


def load_job_manifest(job_dir: Path) -> dict[str, Any]:
    return _shared_run_dir.load_yaml_job_manifest(
        job_dir,
        XTB_JOB_MANIFEST_FILE,
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
    resolved_root = root.expanduser().resolve()
    files: list[Path] = []
    seen_paths: set[Path] = set()
    seen_inodes: set[tuple[int, int]] = set()
    for path in root.glob("*.xyz"):
        resolved = path.resolve()
        if not resolved.is_relative_to(resolved_root):
            raise ValueError(f"xTB input must stay inside its input directory: {path}")
        if resolved.is_file():
            file_status = resolved.stat()
            inode_key = (file_status.st_dev, file_status.st_ino)
            if resolved in seen_paths or inode_key in seen_inodes:
                continue
            seen_paths.add(resolved)
            seen_inodes.add(inode_key)
            files.append(resolved)
    return sorted(files, key=lambda path: path.name.lower())


def _choose_xyz(root: Path, explicit_name: str, *, label: str) -> Path:
    files = _xyz_files(root)
    if explicit_name:
        candidate = (root / explicit_name).resolve()
        if not candidate.is_relative_to(root.expanduser().resolve()):
            raise ValueError(f"{label} file must stay inside its input directory: {candidate}")
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


def _single_frame_atom_sequence(path: Path, *, label: str) -> tuple[str, ...]:
    frames = load_xyz_frames(path)
    if len(frames) != 1:
        raise ValueError(f"{label} must contain exactly one valid finite XYZ frame: {path}")
    atoms = tuple(line.split()[0].casefold() for line in frames[0].atom_lines)
    if not atoms:
        raise ValueError(f"{label} must contain at least one atom: {path}")
    return atoms


def _resolve_path_search_inputs(
    resolved_job_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    reaction_key = _normalize_key(
        _normalize_text(manifest.get("reaction_key")) or resolved_job_dir.name
    )

    reactants_dir = (resolved_job_dir / "reactants").resolve()
    products_dir = (resolved_job_dir / "products").resolve()
    if not reactants_dir.is_relative_to(resolved_job_dir):
        raise ValueError(f"Reactants directory must stay inside the job directory: {reactants_dir}")
    if not products_dir.is_relative_to(resolved_job_dir):
        raise ValueError(f"Products directory must stay inside the job directory: {products_dir}")
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
    reactant_atoms = _single_frame_atom_sequence(reactant_xyz, label="Reactant input")
    product_atoms = _single_frame_atom_sequence(product_xyz, label="Product input")
    if reactant_atoms != product_atoms:
        raise ValueError("xTB path-search endpoints must have identical atom order and elements")

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
    if not candidates_dir.is_relative_to(resolved_job_dir):
        raise ValueError(
            f"Ranking candidates directory must stay inside the job directory: {candidates_dir}"
        )
    if not candidates_dir.exists() or not candidates_dir.is_dir():
        raise ValueError(f"Missing ranking candidates directory: {candidates_dir}")
    candidate_paths = _xyz_files(candidates_dir)
    if not candidate_paths:
        raise ValueError(f"No .xyz candidates found in ranking directory: {candidates_dir}")
    if len(candidate_paths) > MAX_RANKING_CANDIDATES:
        raise ValueError(
            f"Ranking candidate count exceeds the limit of {MAX_RANKING_CANDIDATES}: "
            f"{len(candidate_paths)}"
        )
    max_evaluations_raw = manifest.get(
        "max_ranking_evaluations",
        DEFAULT_MAX_RANKING_EVALUATIONS,
    )
    if (
        isinstance(max_evaluations_raw, bool)
        or not isinstance(max_evaluations_raw, int)
        or not 1 <= max_evaluations_raw <= MAX_RANKING_CANDIDATES
    ):
        raise ValueError(
            f"xTB max_ranking_evaluations must be an integer between 1 and {MAX_RANKING_CANDIDATES}"
        )
    allow_high_cost = manifest.get("allow_high_cost_ranking", False)
    if not isinstance(allow_high_cost, bool):
        raise ValueError("xTB allow_high_cost_ranking must be boolean")
    if max_evaluations_raw > DEFAULT_MAX_RANKING_EVALUATIONS and not allow_high_cost:
        raise ValueError(
            "xTB ranking budgets above the default require allow_high_cost_ranking=true"
        )
    if len(candidate_paths) > max_evaluations_raw:
        raise ValueError(
            f"xTB ranking candidate count {len(candidate_paths)} exceeds the admitted "
            f"budget of {max_evaluations_raw}"
        )
    reference_atoms = _single_frame_atom_sequence(
        candidate_paths[0],
        label="Ranking candidate",
    )
    for candidate_path in candidate_paths[1:]:
        if (
            _single_frame_atom_sequence(candidate_path, label="Ranking candidate")
            != reference_atoms
        ):
            raise ValueError("xTB ranking candidates must have identical atom order and elements")
    molecule_key = _normalize_key(
        _normalize_text(manifest.get("molecule_key"))
        or _normalize_text(manifest.get("reaction_key"))
        or resolved_job_dir.name
    )
    top_n_raw = manifest.get("top_n", 3)
    if isinstance(top_n_raw, bool):
        raise ValueError("xTB ranking top_n must be a positive integer")
    if isinstance(top_n_raw, int):
        top_n = top_n_raw
    elif isinstance(top_n_raw, str) and top_n_raw.strip().isdigit():
        top_n = int(top_n_raw.strip())
    else:
        raise ValueError("xTB ranking top_n must be a positive integer")
    if not 1 <= top_n <= MAX_RANKING_CANDIDATES:
        raise ValueError(f"xTB ranking top_n must be between 1 and {MAX_RANKING_CANDIDATES}")
    input_summary = {
        "candidates_dir": str(candidates_dir),
        "candidate_count": len(candidate_paths),
        "candidate_paths": [str(path) for path in candidate_paths],
        "top_n": top_n,
        "estimated_evaluations": len(candidate_paths),
        "max_ranking_evaluations": max_evaluations_raw,
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
    _single_frame_atom_sequence(input_xyz, label=f"xTB {resolved_type} input")
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
