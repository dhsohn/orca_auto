"""Fan out OptTS+Freq stages from a completed scan_ts_search relaxed scan.

Once the initial ORCA relaxed-scan stage completes, every interior maximum of
its energy profile with prominence above the configured threshold becomes an
OptTS+Freq candidate stage, started from that maximum's numbered ``*.NNN.xyz``
scan artifact. Ranking, per-candidate visibility, and result aggregation come
from the ordinary workflow machinery (queue stages + workflow report).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orca_auto.core.utils.coercion import normalize_text
from orca_auto.flow._orca_stage_materialization import build_materialized_orca_stage
from orca_auto.flow.contracts import WorkflowStageInput
from orca_auto.flow.orchestration.dep_types import OrchestrationDeps
from orca_auto.flow.state import workflow_workspace_internal_engine_paths
from orca_auto.orca.scants import (
    parse_scants_actual_surface,
    scan_profile_interior_maxima,
)

logger = logging.getLogger(__name__)

SCAN_STAGE_ID = "orca_scan_01"
_OPTTS_STAGE_PREFIX = "orca_optts_freq_"
_NO_BARRIER_SCOPE = "scan_ts_search_no_barrier"


def _stage_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    stages = payload.get("stages")
    if not isinstance(stages, list):
        return []
    return [stage for stage in stages if isinstance(stage, dict)]


def _task_payload(stage: dict[str, Any]) -> dict[str, Any]:
    task = stage.get("task")
    if not isinstance(task, dict):
        return {}
    task_payload = task.get("payload")
    return task_payload if isinstance(task_payload, dict) else {}


def _request_parameters(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    request = metadata.get("request")
    if not isinstance(request, dict):
        return {}
    parameters = request.get("parameters")
    return parameters if isinstance(parameters, dict) else {}


def _scan_stage(payload: dict[str, Any]) -> dict[str, Any] | None:
    for stage in _stage_dicts(payload):
        if normalize_text(stage.get("stage_id")) == SCAN_STAGE_ID:
            return stage
    return None


def _existing_optts_stage_count(payload: dict[str, Any]) -> int:
    return sum(
        1
        for stage in _stage_dicts(payload)
        if normalize_text(stage.get("stage_id")).startswith(_OPTTS_STAGE_PREFIX)
    )


def _scan_out_path(scan_stage: dict[str, Any]) -> Path | None:
    artifacts = scan_stage.get("output_artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, dict) and normalize_text(artifact.get("kind")) == (
                "orca_last_out"
            ):
                path_text = normalize_text(artifact.get("path"))
                if path_text:
                    return Path(path_text)
    task_payload = _task_payload(scan_stage)
    selected_inp = normalize_text(task_payload.get("selected_inp"))
    if selected_inp:
        return Path(selected_inp).with_suffix(".out")
    return None


def _scan_selected_inp(scan_stage: dict[str, Any]) -> Path | None:
    selected_inp = normalize_text(_task_payload(scan_stage).get("selected_inp"))
    return Path(selected_inp) if selected_inp else None


def _record_no_barrier_error(
    payload: dict[str, Any],
    *,
    threshold_kcal: float,
    point_count: int,
) -> None:
    metadata = payload.setdefault("metadata", {})
    if not isinstance(metadata, dict) or isinstance(metadata.get("workflow_error"), dict):
        return
    metadata["workflow_error"] = {
        "status": "failed",
        "scope": _NO_BARRIER_SCOPE,
        "stage_id": SCAN_STAGE_ID,
        "reason": "scan_profile_no_barrier",
        "message": (
            f"The completed relaxed scan ({point_count} points) has no interior maximum "
            f"above {threshold_kcal} kcal/mol; there is no TS candidate along this "
            "coordinate."
        ),
    }


def _maximum_candidates(
    scan_stage: dict[str, Any],
    *,
    threshold_kcal: float,
    max_candidates: int,
) -> tuple[list[tuple[Path, int, float]], int]:
    """(numbered xyz, surface point index, prominence) per interior maximum."""
    out_path = _scan_out_path(scan_stage)
    selected_inp = _scan_selected_inp(scan_stage)
    if out_path is None or selected_inp is None:
        return [], 0
    points = parse_scants_actual_surface(out_path)
    maxima = scan_profile_interior_maxima(
        [point.energy for point in points],
        threshold_kcal=threshold_kcal,
    )
    candidates: list[tuple[Path, int, float]] = []
    for list_idx, prominence in maxima[: max(1, max_candidates)]:
        point_index = points[list_idx].index
        candidate_xyz = selected_inp.with_name(f"{selected_inp.stem}.{point_index:03d}.xyz")
        if not candidate_xyz.exists():
            logger.warning(
                "scan_ts_search maximum xyz missing, skipping candidate: %s",
                candidate_xyz,
            )
            continue
        candidates.append((candidate_xyz, point_index, prominence))
    return candidates, len(points)


def append_scan_optts_stages_impl(
    payload: dict[str, Any],
    *,
    workspace_dir: Path,
    deps: OrchestrationDeps | None = None,
) -> bool:
    del deps
    scan_stage = _scan_stage(payload)
    if scan_stage is None:
        return False
    if normalize_text(scan_stage.get("status")) != "completed":
        return False
    if _existing_optts_stage_count(payload):
        return False

    parameters = _request_parameters(payload)
    threshold_kcal = float(parameters.get("barrier_threshold_kcal", 0.5) or 0.5)
    max_candidates = int(parameters.get("max_orca_stages", 5) or 5)
    candidates, point_count = _maximum_candidates(
        scan_stage,
        threshold_kcal=threshold_kcal,
        max_candidates=max_candidates,
    )
    if not candidates:
        _record_no_barrier_error(
            payload,
            threshold_kcal=threshold_kcal,
            point_count=point_count,
        )
        return False

    orca_paths = workflow_workspace_internal_engine_paths(workspace_dir, engine="orca")
    workflow_id = normalize_text(payload.get("workflow_id"))
    reaction_key = normalize_text(payload.get("reaction_key"))
    route_line = normalize_text(parameters.get("orca_optts_route_line")) or (
        "! OptTS Freq r2scan-3c TightSCF"
    )
    for rank, (candidate_xyz, point_index, prominence) in enumerate(candidates, start=1):
        candidate = WorkflowStageInput(
            source_job_id="",
            source_job_type="relaxed_scan",
            reaction_key=reaction_key,
            selected_input_xyz=str(candidate_xyz),
            rank=rank,
            kind="scan_maximum",
            artifact_path=str(candidate_xyz),
            selected=True,
            score=float(prominence),
            metadata={
                "scan_stage_id": SCAN_STAGE_ID,
                "surface_point_index": int(point_index),
                "prominence_kcal": float(prominence),
            },
        )
        stage = build_materialized_orca_stage(
            workflow_id=workflow_id,
            template_name="scan_ts_search",
            stage_id=f"{_OPTTS_STAGE_PREFIX}{rank:02d}",
            stage_key=f"{rank:02d}_scan_maximum",
            stage_root_name="",
            workspace_dir=orca_paths["allowed_root"],
            input_artifact_kind="scan_maximum",
            candidate=candidate,
            task_kind="optts_freq",
            route_line=route_line,
            charge=int(parameters.get("charge", 0) or 0),
            multiplicity=int(parameters.get("multiplicity", 1) or 1),
            max_cores=int(parameters.get("max_cores", 8) or 8),
            max_memory_gb=int(parameters.get("max_memory_gb", 32) or 32),
            priority=int(parameters.get("priority", 10) or 10),
            xyz_filename="ts_guess.xyz",
            inp_filename="ts_guess.inp",
        )
        payload.setdefault("stages", []).append(stage.to_dict())
    return True


__all__ = ["append_scan_optts_stages_impl"]
