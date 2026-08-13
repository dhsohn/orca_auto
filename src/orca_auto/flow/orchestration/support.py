from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from orca_auto.core.config.files import YAML_CONFIG_LOAD_EXCEPTIONS
from orca_auto.core.utils import normalize_text
from orca_auto.flow.contracts import XtbDownstreamPolicy
from orca_auto.flow.contracts.workflow import workflow_stage_metadata
from orca_auto.flow.contracts.xtb import geometry_validation_passed
from orca_auto.flow.orchestration.services import (
    OrchestrationServices,
    resolve_orchestration_services,
)

if TYPE_CHECKING:
    from orca_auto.flow.contracts.xtb import XtbArtifactContract


def _runtime_paths_for_engine(
    config_path: str,
    *,
    engine: str,
    services: OrchestrationServices | None = None,
) -> dict[str, Path]:
    o = resolve_orchestration_services(services)
    return o.engines.engine_runtime_paths(config_path, engine=engine)


def submission_target_impl(stage: dict[str, Any]) -> str:
    stage_metadata = stage.get("metadata")
    if isinstance(stage_metadata, dict):
        queue_id = normalize_text(stage_metadata.get("queue_id"))
        if queue_id:
            return queue_id
    task = stage.get("task")
    if isinstance(task, dict):
        submission_result = task.get("submission_result")
        if isinstance(submission_result, dict):
            parsed = submission_result.get("parsed_stdout")
            if isinstance(parsed, dict):
                for key in ("job_id", "queue_id"):
                    value = normalize_text(parsed.get(key))
                    if value:
                        return value
    return ""


def load_config_root_impl(
    config_path: str | None,
    *,
    engine: str = "orca",
    services: OrchestrationServices | None = None,
) -> Path | None:
    text = normalize_text(config_path)
    if not text:
        return None
    try:
        return _runtime_paths_for_engine(text, engine=engine, services=services)["allowed_root"]
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        return None


def required_stage_budget(params: dict[str, Any], key: str) -> Any:
    """Fail closed when a persisted stage budget is missing.

    Creation always records the stage budgets in the durable payload; a
    payload without one is corrupt or hand-edited, and guessing a budget
    would silently change how far a stored workflow expands.
    """

    value = params.get(key)
    if value is None:
        raise ValueError(
            f"workflow payload is missing parameters.{key}; refusing to guess a stage budget"
        )
    return value


def select_valid_ts_guess_inputs(
    services: OrchestrationServices,
    contract: XtbArtifactContract,
) -> list[Any]:
    """Return the ts_guess candidates whose geometry validation did not reject them.

    Every ts_guess is requested before filtering: capping the request first would
    discard a whole contract whose top-ranked guess is geometry-invalid even when
    a lower-ranked one is usable.
    """
    max_candidate_count = max(
        1,
        len(contract.candidate_details),
        len(contract.selected_candidate_paths),
    )
    inputs = services.engines.select_xtb_downstream_inputs(
        contract,
        policy=XtbDownstreamPolicy.build(
            preferred_kinds=("ts_guess",),
            allowed_kinds=("ts_guess",),
            max_candidates=max_candidate_count,
            selected_only=False,
        ),
        require_geometry=True,
    )
    return [candidate for candidate in inputs if candidate.geometry_validated]


def reaction_ts_guess_error_impl(
    contract: XtbArtifactContract,
    *,
    services: OrchestrationServices | None = None,
) -> dict[str, str]:
    o = resolve_orchestration_services(services)
    details = sorted(
        [
            item
            for item in contract.candidate_details
            if normalize_text(item.kind) == "ts_guess" and normalize_text(item.path)
        ],
        key=lambda item: item.rank if item.rank > 0 else 10_000,
    )
    if not details:
        return {
            "reason": "xtb_ts_guess_missing",
            "message": "xTB path_search did not produce a ts_guess candidate (xtbpath_ts.xyz); refusing ORCA handoff.",
        }
    candidate = details[0]
    if candidate.metadata.get("geometry_valid") is False:
        validation = candidate.metadata.get("geometry_validation")
        reasons = "; ".join(
            str(reason)
            for reason in (validation.get("reasons", []) if isinstance(validation, dict) else [])
        )
        detail_text = f" ({reasons})" if reasons else ""
        return {
            "reason": "xtb_ts_guess_geometry_invalid",
            "message": (
                "xTB produced xtbpath_ts.xyz but its geometry failed validation against "
                f"the reactant/product endpoints{detail_text}; refusing ORCA handoff."
            ),
        }
    if not geometry_validation_passed(candidate.metadata):
        return {
            "reason": "xtb_ts_guess_geometry_unvalidated",
            "message": (
                "xTB produced xtbpath_ts.xyz without an explicit successful geometry validation "
                "against the reactant/product endpoints; refusing ORCA handoff."
            ),
        }
    _, metadata = o.engines.choose_orca_geometry_frame(candidate.path, candidate_kind="ts_guess")
    selection_reason = normalize_text(metadata.get("selection_reason")) or "invalid_or_empty_xyz"
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


def reaction_orca_source_candidate_path_impl(stage: dict[str, Any]) -> str:
    task = stage.get("task")
    if isinstance(task, dict):
        task_metadata = task.get("metadata")
        if isinstance(task_metadata, dict):
            path = normalize_text(task_metadata.get("source_candidate_path"))
            if path:
                return path
    for artifact in stage.get("input_artifacts", []):
        if not isinstance(artifact, dict):
            continue
        if normalize_text(artifact.get("kind")) != "xtb_candidate":
            continue
        path = normalize_text(artifact.get("path"))
        if path:
            return path
    return ""


def clear_reaction_xtb_handoff_error_if_recovering_impl(payload: dict[str, Any]) -> None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return
    workflow_error = metadata.get("workflow_error")
    if not isinstance(workflow_error, dict):
        return
    if normalize_text(workflow_error.get("scope")) != "reaction_ts_search_xtb_handoff":
        return
    for stage in payload.get("stages", []):
        if not isinstance(stage, dict):
            continue
        task = stage.get("task")
        if not isinstance(task, dict) or normalize_text(task.get("engine")) != "xtb":
            continue
        stage_status = normalize_text(stage.get("status")).lower()
        handoff_status = normalize_text(
            workflow_stage_metadata(stage).get("reaction_handoff_status")
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
    "reaction_orca_source_candidate_path_impl",
    "reaction_ts_guess_error_impl",
    "submission_target_impl",
]
