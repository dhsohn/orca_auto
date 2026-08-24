"""Scan-driven stage progression for the scan_ts_search workflow.

Ports the recovery strategies proven in the ScanTS retry chain into proper
workflow stages:

1. Forward fan-out — once the forward relaxed scan(s) complete, every interior
   maximum of the combined forward profile with prominence above the threshold
   becomes an OptTS+Freq candidate stage.
2. Scan extension — a completed forward profile with NO interior maximum gets
   one more forward scan stage extending the coordinate past the previous
   endpoint (ScanTS continuation math: max(6, 20% of the range) extra points),
   up to ``max_scan_extensions``; only then is ``scan_profile_no_barrier``
   recorded.
3. Reverse rescue — when every forward candidate finishes without verifying a
   TS, a reverse scan stage walks the full coordinate back from the forward
   endpoint geometry (scan hysteresis gives different maximum geometries), and
   its interior maxima fan out as a second candidate batch. Only when those
   are exhausted too does the workflow fail.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.statuses import (
    STATUS_CANCEL_FAILED,
    STATUS_CANCEL_REQUESTED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    is_stage_terminal_status,
)
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.flow._orca_stage_materialization import build_materialized_orca_stage
from orca_auto.flow.contracts import WorkflowStageInput
from orca_auto.flow.contracts.workflow import (
    required_route_line,
    workflow_request_parameters,
)
from orca_auto.flow.orchestration.charge_spin import strict_int
from orca_auto.flow.orchestration.support import required_stage_budget
from orca_auto.flow.orchestration.template_builders import scan_geom_block
from orca_auto.flow.xyz_utils import validated_xyz_atom_count
from orca_auto.orca.input_artifacts import derive_selected_input_xyz
from orca_auto.orca.scants import (
    ScanCoordinateSpec,
    ScanTSSurfacePoint,
    format_scan_coordinate,
    parse_scan_coordinate,
    parse_scants_actual_surface,
    scan_profile_interior_maxima,
    validate_scan_coordinate,
)

logger = logging.getLogger(__name__)

REVERSE_SCAN_STAGE_ID = "orca_scan_reverse_01"
_FORWARD_SCAN_STAGE_PREFIX = "orca_scan_"
_OPTTS_STAGE_PREFIX = "orca_optts_freq_"
_NO_BARRIER_SCOPE = "scan_ts_search_no_barrier"
_EXHAUSTED_SCOPE = "scan_ts_search_candidates_exhausted"
_MIN_EXTENSION_STEPS = 6
_EXTENSION_FRACTION = 0.2


def _stage_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    stages = payload.get("stages")
    if not isinstance(stages, list):
        return []
    return [stage for stage in stages if isinstance(stage, dict)]


def _next_stage_key(payload: dict[str, Any], kind: str) -> str:
    """Workflow-ordered stage directory name: 01_scan, 02_scan_maximum, ...

    scan_ts_search stages live directly under the workspace (no engine root),
    so the key numbering runs across the whole workflow in creation order.
    """
    return f"{len(_stage_dicts(payload)) + 1:02d}_{kind}"


def _stage_status(stage: dict[str, Any]) -> str:
    return normalize_text(stage.get("status")).lower()


def _stage_metadata(stage: dict[str, Any]) -> dict[str, Any]:
    metadata = stage.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _task_payload(stage: dict[str, Any]) -> dict[str, Any]:
    task = stage.get("task")
    if not isinstance(task, dict):
        return {}
    task_payload = task.get("payload")
    return task_payload if isinstance(task_payload, dict) else {}


def _task_kind(stage: dict[str, Any]) -> str:
    task = stage.get("task")
    if not isinstance(task, dict):
        return ""
    return normalize_text(task.get("task_kind"))


def _scan_direction(stage: dict[str, Any]) -> str:
    return normalize_text(_stage_metadata(stage).get("scan_direction")) or "forward"


def _scan_stages(payload: dict[str, Any], *, direction: str) -> list[dict[str, Any]]:
    return [
        stage
        for stage in _stage_dicts(payload)
        if _task_kind(stage) == "relaxed_scan" and _scan_direction(stage) == direction
    ]


def _optts_stages(payload: dict[str, Any], *, direction: str) -> list[dict[str, Any]]:
    stages = []
    for stage in _stage_dicts(payload):
        if not normalize_text(stage.get("stage_id")).startswith(_OPTTS_STAGE_PREFIX):
            continue
        candidate_direction = ""
        artifacts = stage.get("input_artifacts")
        if isinstance(artifacts, list) and artifacts and isinstance(artifacts[0], dict):
            artifact_metadata = artifacts[0].get("metadata")
            if isinstance(artifact_metadata, dict):
                candidate_direction = normalize_text(artifact_metadata.get("scan_direction"))
        if (candidate_direction or "forward") == direction:
            stages.append(stage)
    return stages


_CANCEL_STATUSES = frozenset({STATUS_CANCELLED, STATUS_CANCEL_REQUESTED, STATUS_CANCEL_FAILED})


def _all_terminal_none_verified(stages: list[dict[str, Any]]) -> bool:
    """True when every candidate reached a non-verifying terminal state.

    A cancellation in progress transitions candidates to cancelled without a
    TS verdict; those must NOT count as "failed to verify a TS" — otherwise a
    reverse scan (or an exhaustion error) would be appended mid-cancellation,
    and since terminal/cancel sync runs with submit_ready=False the new stage
    never submits and the workflow can get stuck in cancel_requested.
    """
    if not stages:
        return False
    statuses = [_stage_status(stage) for stage in stages]
    if any(status in _CANCEL_STATUSES for status in statuses):
        return False
    return all(is_stage_terminal_status(status) for status in statuses) and not any(
        status == STATUS_COMPLETED for status in statuses
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
    selected_inp = normalize_text(_task_payload(scan_stage).get("selected_inp"))
    if selected_inp:
        return Path(selected_inp).with_suffix(".out")
    return None


def _scan_selected_inp(scan_stage: dict[str, Any]) -> Path | None:
    selected_inp = normalize_text(_task_payload(scan_stage).get("selected_inp"))
    return Path(selected_inp) if selected_inp else None


def _scan_atom_count(scan_stage: dict[str, Any]) -> int:
    task_payload = _task_payload(scan_stage)
    xyz_path = normalize_text(task_payload.get("selected_input_xyz"))
    if not xyz_path:
        xyz_path = derive_selected_input_xyz(task_payload.get("selected_inp"))
    if not xyz_path:
        raise ValueError(
            "relaxed scan coordinate cannot be validated without its selected input XYZ"
        )
    return validated_xyz_atom_count(xyz_path)


def _scan_spec(scan_stage: dict[str, Any]) -> ScanCoordinateSpec | None:
    coordinate = normalize_text(_stage_metadata(scan_stage).get("scan_coordinate"))
    if not coordinate:
        return None
    canonical = validate_scan_coordinate(
        coordinate,
        atom_count=_scan_atom_count(scan_stage),
    )
    spec = parse_scan_coordinate(canonical)
    assert spec is not None
    return spec


def _scan_surface(scan_stage: dict[str, Any]) -> list[ScanTSSurfacePoint]:
    out_path = _scan_out_path(scan_stage)
    if out_path is None:
        return []
    return parse_scants_actual_surface(out_path)


def _scan_endpoint_xyz(scan_stage: dict[str, Any]) -> Path | None:
    """The last numbered scan-point geometry of a completed scan stage."""
    selected_inp = _scan_selected_inp(scan_stage)
    if selected_inp is None:
        return None
    point_count = len(_scan_surface(scan_stage))
    if point_count == 0:
        spec = _scan_spec(scan_stage)
        if spec is None:
            return None
        point_count = spec.points
    candidate = selected_inp.with_name(f"{selected_inp.stem}.{point_count:03d}.xyz")
    return candidate if candidate.exists() else None


def _record_workflow_error(
    payload: dict[str, Any],
    *,
    scope: str,
    stage_id: str,
    reason: str,
    message: str,
) -> None:
    metadata = payload.setdefault("metadata", {})
    if not isinstance(metadata, dict) or isinstance(metadata.get("workflow_error"), dict):
        return
    metadata["workflow_error"] = {
        "status": "failed",
        "scope": scope,
        "stage_id": stage_id,
        "reason": reason,
        "message": message,
    }


def _combined_maximum_candidates(
    scan_stages: list[dict[str, Any]],
    *,
    threshold_kcal: float,
    max_candidates: int,
) -> tuple[list[tuple[dict[str, Any], Path, int, float]], int]:
    """(owning stage, numbered xyz, point index, prominence) per interior maximum.

    Maxima are detected on the CONCATENATED profile of the given scan stages
    (in stage order), so a barrier revealed by an extension segment — or
    sitting at a segment junction — is still found and mapped back to the
    owning stage's ``*.NNN.xyz`` artifact.
    """
    energies: list[float] = []
    refs: list[tuple[dict[str, Any], int]] = []
    for stage in scan_stages:
        for point in _scan_surface(stage):
            energies.append(point.energy)
            refs.append((stage, point.index))
    maxima = scan_profile_interior_maxima(energies, threshold_kcal=threshold_kcal)
    candidates: list[tuple[dict[str, Any], Path, int, float]] = []
    for list_idx, prominence in maxima[: max(1, max_candidates)]:
        stage, point_index = refs[list_idx]
        selected_inp = _scan_selected_inp(stage)
        if selected_inp is None:
            continue
        candidate_xyz = selected_inp.with_name(f"{selected_inp.stem}.{point_index:03d}.xyz")
        if not candidate_xyz.exists():
            logger.warning(
                "scan_ts_search maximum xyz missing, skipping candidate: %s",
                candidate_xyz,
            )
            continue
        candidates.append((stage, candidate_xyz, point_index, prominence))
    return candidates, len(energies)


def _append_optts_candidate_stages(
    payload: dict[str, Any],
    *,
    workspace_dir: Path,
    parameters: dict[str, Any],
    candidates: list[tuple[dict[str, Any], Path, int, float]],
    direction: str,
) -> None:
    workflow_id = normalize_text(payload.get("workflow_id"))
    reaction_key = normalize_text(payload.get("reaction_key"))
    route_line = required_route_line(parameters, "orca_optts_route_line")
    existing = len(_optts_stages(payload, direction="forward")) + len(
        _optts_stages(payload, direction="reverse")
    )
    for offset, (scan_stage, candidate_xyz, point_index, prominence) in enumerate(
        candidates, start=1
    ):
        stage_number = existing + offset
        candidate = WorkflowStageInput(
            source_job_id="",
            source_job_type="relaxed_scan",
            reaction_key=reaction_key,
            selected_input_xyz=str(candidate_xyz),
            rank=offset,
            kind="scan_maximum",
            artifact_path=str(candidate_xyz),
            selected=True,
            score=float(prominence),
            metadata={
                "scan_stage_id": normalize_text(scan_stage.get("stage_id")),
                "scan_direction": direction,
                "surface_point_index": int(point_index),
                "prominence_kcal": float(prominence),
            },
        )
        stage = build_materialized_orca_stage(
            workflow_id=workflow_id,
            template_name="scan_ts_search",
            stage_id=f"{_OPTTS_STAGE_PREFIX}{stage_number:02d}",
            stage_key=_next_stage_key(payload, "scan_maximum"),
            workspace_dir=workspace_dir,
            input_artifact_kind="scan_maximum",
            candidate=candidate,
            task_kind="optts_freq",
            route_line=route_line,
            charge=strict_int(parameters.get("charge", 0), field="charge"),
            multiplicity=strict_int(
                parameters.get("multiplicity", 1), field="multiplicity", minimum=1
            ),
            max_cores=int(parameters.get("max_cores", 8) or 8),
            max_memory_gb=int(parameters.get("max_memory_gb", 32) or 32),
            priority=normalize_queue_priority(parameters.get("priority")),
            xyz_filename="ts_guess.xyz",
            inp_filename="ts_guess.inp",
        )
        payload.setdefault("stages", []).append(stage.to_dict())


def _append_scan_stage(
    payload: dict[str, Any],
    *,
    workspace_dir: Path,
    parameters: dict[str, Any],
    stage_id: str,
    stage_kind: str,
    spec: ScanCoordinateSpec,
    direction: str,
    start_xyz: Path,
) -> None:
    scan_coordinate = validate_scan_coordinate(
        format_scan_coordinate(spec),
        atom_count=validated_xyz_atom_count(start_xyz),
    )
    candidate = WorkflowStageInput(
        source_job_id="",
        source_job_type="relaxed_scan",
        reaction_key=normalize_text(payload.get("reaction_key")),
        selected_input_xyz=str(start_xyz),
        rank=1,
        kind="scan_endpoint",
        artifact_path=str(start_xyz),
        selected=True,
        score=0.0,
        metadata={"scan_direction": direction},
    )
    stage = build_materialized_orca_stage(
        workflow_id=normalize_text(payload.get("workflow_id")),
        template_name="scan_ts_search",
        stage_id=stage_id,
        stage_key=_next_stage_key(payload, stage_kind),
        workspace_dir=workspace_dir,
        input_artifact_kind="scan_endpoint",
        candidate=candidate,
        task_kind="relaxed_scan",
        route_line=required_route_line(parameters, "orca_route_line"),
        charge=strict_int(parameters.get("charge", 0), field="charge"),
        multiplicity=strict_int(parameters.get("multiplicity", 1), field="multiplicity", minimum=1),
        max_cores=int(parameters.get("max_cores", 8) or 8),
        max_memory_gb=int(parameters.get("max_memory_gb", 32) or 32),
        priority=normalize_queue_priority(parameters.get("priority")),
        xyz_filename="scan_input.xyz",
        inp_filename="scan.inp",
        geom_block=scan_geom_block(scan_coordinate),
    )
    stage_dict = stage.to_dict()
    stage_metadata = stage_dict.setdefault("metadata", {})
    if isinstance(stage_metadata, dict):
        # Scan stages are prerequisites, not candidates: their failure fails
        # the workflow (see the status reducer's workflow_fatal handling).
        stage_metadata["workflow_fatal"] = True
        stage_metadata["scan_direction"] = direction
        stage_metadata["scan_coordinate"] = scan_coordinate
    payload.setdefault("stages", []).append(stage_dict)


def _extension_spec(previous: ScanCoordinateSpec) -> ScanCoordinateSpec | None:
    """Continue the scan past the previous endpoint (ScanTS continuation math)."""
    if previous.points <= 1:
        return None
    step = (previous.end - previous.start) / (previous.points - 1)
    if step == 0:
        return None
    extension_steps = max(
        _MIN_EXTENSION_STEPS,
        round((previous.points - 1) * _EXTENSION_FRACTION),
    )
    return ScanCoordinateSpec(
        kind=previous.kind,
        atoms=previous.atoms,
        start=previous.end + step,
        end=previous.end + step * extension_steps,
        points=extension_steps,
    )


def _reverse_spec(forward_scans: list[dict[str, Any]]) -> ScanCoordinateSpec | None:
    """Walk the full forward range (including extensions) back to its start."""
    specs = [_scan_spec(stage) for stage in forward_scans]
    if not specs or any(spec is None for spec in specs):
        return None
    first, last = specs[0], specs[-1]
    assert first is not None and last is not None
    return ScanCoordinateSpec(
        kind=first.kind,
        atoms=first.atoms,
        start=last.end,
        end=first.start,
        points=sum(spec.points for spec in specs if spec is not None),
    )


def _advance_forward(
    payload: dict[str, Any],
    *,
    workspace_dir: Path,
    parameters: dict[str, Any],
    forward_scans: list[dict[str, Any]],
) -> bool:
    latest = forward_scans[-1]
    if _stage_status(latest) != "completed":
        return False
    threshold_kcal = float(parameters.get("barrier_threshold_kcal", 0.5) or 0.5)
    max_candidates = strict_int(
        required_stage_budget(parameters, "max_orca_stages"),
        field="max_orca_stages",
        minimum=1,
    )
    candidates, point_count = _combined_maximum_candidates(
        forward_scans,
        threshold_kcal=threshold_kcal,
        max_candidates=max_candidates,
    )
    if candidates:
        _append_optts_candidate_stages(
            payload,
            workspace_dir=workspace_dir,
            parameters=parameters,
            candidates=candidates,
            direction="forward",
        )
        return True

    max_extensions = strict_int(
        required_stage_budget(parameters, "max_scan_extensions"),
        field="max_scan_extensions",
        minimum=0,
    )
    extensions_used = len(forward_scans) - 1
    if extensions_used < max_extensions:
        previous_spec = _scan_spec(latest)
        if previous_spec is None and len(forward_scans) == 1:
            canonical = validate_scan_coordinate(
                parameters.get("scan_coordinate"),
                atom_count=_scan_atom_count(latest),
            )
            previous_spec = parse_scan_coordinate(canonical)
            assert previous_spec is not None
        endpoint_xyz = _scan_endpoint_xyz(latest)
        extension = _extension_spec(previous_spec) if previous_spec is not None else None
        if extension is not None and endpoint_xyz is not None:
            next_index = len(forward_scans) + 1
            _append_scan_stage(
                payload,
                workspace_dir=workspace_dir,
                parameters=parameters,
                stage_id=f"{_FORWARD_SCAN_STAGE_PREFIX}{next_index:02d}",
                stage_kind="scan_extension",
                spec=extension,
                direction="forward",
                start_xyz=endpoint_xyz,
            )
            return True
        logger.warning("scan_ts_search extension not possible; recording no-barrier error")

    _record_workflow_error(
        payload,
        scope=_NO_BARRIER_SCOPE,
        stage_id=normalize_text(latest.get("stage_id")),
        reason="scan_profile_no_barrier",
        message=(
            f"The forward relaxed scan ({point_count} points across "
            f"{len(forward_scans)} segment(s)) has no interior maximum above "
            f"{threshold_kcal} kcal/mol; there is no TS candidate along this coordinate."
        ),
    )
    return False


def _advance_reverse(
    payload: dict[str, Any],
    *,
    workspace_dir: Path,
    parameters: dict[str, Any],
    forward_scans: list[dict[str, Any]],
) -> bool:
    reverse_scans = _scan_stages(payload, direction="reverse")
    if not reverse_scans:
        reverse = _reverse_spec(forward_scans)
        endpoint_xyz = _scan_endpoint_xyz(forward_scans[-1])
        if reverse is None or endpoint_xyz is None:
            _record_workflow_error(
                payload,
                scope=_EXHAUSTED_SCOPE,
                stage_id=normalize_text(forward_scans[-1].get("stage_id")),
                reason="ts_candidates_exhausted",
                message=(
                    "Every forward OptTS candidate finished without verifying a TS "
                    "and no reverse scan could be built."
                ),
            )
            return False
        _append_scan_stage(
            payload,
            workspace_dir=workspace_dir,
            parameters=parameters,
            stage_id=REVERSE_SCAN_STAGE_ID,
            stage_kind="scan_reverse",
            spec=reverse,
            direction="reverse",
            start_xyz=endpoint_xyz,
        )
        return True

    latest_reverse = reverse_scans[-1]
    if _stage_status(latest_reverse) != "completed":
        return False
    reverse_optts = _optts_stages(payload, direction="reverse")
    if not reverse_optts:
        threshold_kcal = float(parameters.get("barrier_threshold_kcal", 0.5) or 0.5)
        max_candidates = strict_int(
            required_stage_budget(parameters, "max_orca_stages"),
            field="max_orca_stages",
            minimum=1,
        )
        candidates, point_count = _combined_maximum_candidates(
            reverse_scans,
            threshold_kcal=threshold_kcal,
            max_candidates=max_candidates,
        )
        if candidates:
            _append_optts_candidate_stages(
                payload,
                workspace_dir=workspace_dir,
                parameters=parameters,
                candidates=candidates,
                direction="reverse",
            )
            return True
        _record_workflow_error(
            payload,
            scope=_EXHAUSTED_SCOPE,
            stage_id=normalize_text(latest_reverse.get("stage_id")),
            reason="ts_candidates_exhausted",
            message=(
                f"The reverse relaxed scan ({point_count} points) has no interior "
                "maximum above the threshold; forward and reverse candidates are "
                "exhausted."
            ),
        )
        return False
    if _all_terminal_none_verified(reverse_optts):
        _record_workflow_error(
            payload,
            scope=_EXHAUSTED_SCOPE,
            stage_id=normalize_text(reverse_optts[-1].get("stage_id")),
            reason="ts_candidates_exhausted",
            message=("Every forward and reverse OptTS candidate finished without verifying a TS."),
        )
    return False


def append_scan_optts_stages_impl(
    payload: dict[str, Any],
    *,
    workspace_dir: Path,
) -> bool:
    forward_scans = _scan_stages(payload, direction="forward")
    if not forward_scans:
        return False
    parameters = workflow_request_parameters(payload)

    forward_optts = _optts_stages(payload, direction="forward")
    if not forward_optts:
        return _advance_forward(
            payload,
            workspace_dir=workspace_dir,
            parameters=parameters,
            forward_scans=forward_scans,
        )
    if _all_terminal_none_verified(forward_optts):
        return _advance_reverse(
            payload,
            workspace_dir=workspace_dir,
            parameters=parameters,
            forward_scans=forward_scans,
        )
    return False


__all__ = ["append_scan_optts_stages_impl"]
