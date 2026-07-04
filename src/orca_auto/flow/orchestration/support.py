from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from orca_auto.core.config.files import YAML_CONFIG_LOAD_EXCEPTIONS
from orca_auto.flow.contracts.workflow import workflow_stage_metadata, workflow_task_payload_dict
from orca_auto.flow.orchestration.dep_types import OrchestrationDeps
from orca_auto.flow.orchestration.deps import (
    orchestration_context as _orchestration_context,
)

if TYPE_CHECKING:
    from orca_auto.flow.contracts.xtb import XtbArtifactContract


def _runtime_paths_for_engine(
    config_path: str,
    *,
    engine: str,
    deps: OrchestrationDeps | None = None,
) -> dict[str, Path]:
    o = _orchestration_context(deps)
    return o.engines.engine_runtime_paths(config_path, engine=engine)


def submission_target_impl(stage: dict[str, Any], *, deps: OrchestrationDeps | None = None) -> str:
    o = _orchestration_context(deps)
    stage_metadata = stage.get("metadata")
    if isinstance(stage_metadata, dict):
        queue_id = o.stages.support._normalize_text(stage_metadata.get("queue_id"))
        if queue_id:
            return queue_id
    task = stage.get("task")
    if isinstance(task, dict):
        submission_result = task.get("submission_result")
        if isinstance(submission_result, dict):
            parsed = submission_result.get("parsed_stdout")
            if isinstance(parsed, dict):
                for key in ("job_id", "queue_id"):
                    value = o.stages.support._normalize_text(parsed.get(key))
                    if value:
                        return value
    return ""


def load_config_root_impl(
    config_path: str | None,
    *,
    engine: str = "orca",
    deps: OrchestrationDeps | None = None,
) -> Path | None:
    o = _orchestration_context(deps)
    text = o.stages.support._normalize_text(config_path)
    if not text:
        return None
    try:
        return _runtime_paths_for_engine(text, engine=engine, deps=o)["allowed_root"]
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        return None


def stage_metadata_impl(stage: dict[str, Any]) -> dict[str, Any]:
    return workflow_stage_metadata(stage)


def task_payload_dict_impl(task: dict[str, Any]) -> dict[str, Any]:
    return workflow_task_payload_dict(task)


def reaction_ts_guess_error_impl(
    contract: XtbArtifactContract, *, deps: OrchestrationDeps | None = None
) -> dict[str, str]:
    o = _orchestration_context(deps)
    details = sorted(
        [
            item
            for item in contract.candidate_details
            if o.stages.support._normalize_text(item.kind) == "ts_guess"
            and o.stages.support._normalize_text(item.path)
        ],
        key=lambda item: item.rank if item.rank > 0 else 10_000,
    )
    if not details:
        return {
            "reason": "xtb_ts_guess_missing",
            "message": "xTB path_search did not produce a ts_guess candidate (xtbpath_ts.xyz); refusing ORCA handoff.",
        }
    candidate = details[0]
    _, metadata = o.engines.choose_orca_geometry_frame(candidate.path, candidate_kind="ts_guess")
    selection_reason = (
        o.stages.support._normalize_text(metadata.get("selection_reason")) or "invalid_or_empty_xyz"
    )
    reason_map = {
        "invalid_or_empty_xyz": "xtb_ts_guess_invalid",
        "ts_guess_requires_single_frame": "xtb_ts_guess_not_single_geometry",
    }
    reason = reason_map.get(selection_reason, "xtb_ts_guess_invalid")
    if selection_reason == "ts_guess_requires_single_frame":
        message = "xTB produced xtbpath_ts.xyz but it is not a single-geometry TS guess; refusing ORCA handoff."
    else:
        message = "xTB produced xtbpath_ts.xyz but it is empty or not a valid XYZ geometry; refusing ORCA handoff."
    return {
        "reason": reason,
        "message": message,
    }


def reaction_orca_source_candidate_path_impl(
    stage: dict[str, Any], *, deps: OrchestrationDeps | None = None
) -> str:
    o = _orchestration_context(deps)
    task = stage.get("task")
    if isinstance(task, dict):
        task_metadata = task.get("metadata")
        if isinstance(task_metadata, dict):
            path = o.stages.support._normalize_text(task_metadata.get("source_candidate_path"))
            if path:
                return path
    for artifact in stage.get("input_artifacts", []):
        if not isinstance(artifact, dict):
            continue
        if o.stages.support._normalize_text(artifact.get("kind")) != "xtb_candidate":
            continue
        path = o.stages.support._normalize_text(artifact.get("path"))
        if path:
            return path
    return ""


def reaction_orca_allows_next_candidate_impl(
    stage: dict[str, Any], *, deps: OrchestrationDeps | None = None
) -> bool:
    o = _orchestration_context(deps)
    status = o.stages.support._normalize_text(stage.get("status")).lower()
    if status not in {"failed", "cancel_failed"}:
        return False
    metadata = o.stages.support._stage_metadata(stage)
    if o.stages.support._normalize_text(metadata.get("reaction_candidate_status")) == "superseded":
        return False
    analyzer_status = o.stages.support._normalize_text(metadata.get("analyzer_status")).lower()
    latest_attempt_status = o.stages.support._normalize_text(
        metadata.get("orca_latest_attempt_status")
    ).lower()
    reason = o.stages.support._normalize_text(metadata.get("reason")).lower()
    allowed = {
        "ts_not_found",
        "geom_not_converged",
        "incomplete",
        "error_scf",
        "error_scfgrad_abort",
    }
    return any(item in allowed for item in (analyzer_status, latest_attempt_status, reason) if item)


def clear_reaction_xtb_handoff_error_if_recovering_impl(
    payload: dict[str, Any], *, deps: OrchestrationDeps | None = None
) -> None:
    o = _orchestration_context(deps)
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return
    workflow_error = metadata.get("workflow_error")
    if not isinstance(workflow_error, dict):
        return
    if (
        o.stages.support._normalize_text(workflow_error.get("scope"))
        != "reaction_ts_search_xtb_handoff"
    ):
        return
    for stage in payload.get("stages", []):
        if not isinstance(stage, dict):
            continue
        task = stage.get("task")
        if (
            not isinstance(task, dict)
            or o.stages.support._normalize_text(task.get("engine")) != "xtb"
        ):
            continue
        stage_status = o.stages.support._normalize_text(stage.get("status")).lower()
        handoff_status = o.stages.support._normalize_text(
            o.stages.support._stage_metadata(stage).get("reaction_handoff_status")
        ).lower()
        if (
            stage_status in {"planned", "queued", "running", "submitted"}
            or handoff_status == "retrying"
        ):
            metadata.pop("workflow_error", None)
            return


__all__ = [
    "clear_reaction_xtb_handoff_error_if_recovering_impl",
    "load_config_root_impl",
    "reaction_orca_allows_next_candidate_impl",
    "reaction_orca_source_candidate_path_impl",
    "reaction_ts_guess_error_impl",
    "stage_metadata_impl",
    "submission_target_impl",
    "task_payload_dict_impl",
]
