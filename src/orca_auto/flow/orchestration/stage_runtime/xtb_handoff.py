from __future__ import annotations

from typing import Any

from orca_auto.flow.orchestration.dep_types import OrchestrationDeps
from orca_auto.flow.orchestration.stage_runtime.shared import _orchestration_context


def xtb_handoff_status_impl(
    contract: Any, *, deps: OrchestrationDeps | None = None
) -> dict[str, str]:
    o = _orchestration_context(deps)
    inputs = o.engines.select_xtb_downstream_inputs(
        contract,
        policy=o.contracts.XtbDownstreamPolicy.build(
            preferred_kinds=("ts_guess",),
            allowed_kinds=("ts_guess",),
            max_candidates=1,
            selected_only=False,
        ),
        require_geometry=True,
    )
    inputs = tuple(item for item in inputs if not item.geometry_invalid)
    if inputs:
        return {
            "status": "ready",
            "reason": "",
            "message": "",
            "artifact_path": o.stages.support._normalize_text(inputs[0].artifact_path),
        }
    error = o.stages.support._reaction_ts_guess_error(contract)
    return {
        "status": "failed",
        "reason": error["reason"],
        "message": error["message"],
        "artifact_path": "",
    }


def stage_has_xtb_candidates_impl(
    stage: dict[str, Any], *, deps: OrchestrationDeps | None = None
) -> bool:
    o = _orchestration_context(deps)
    artifacts = stage.get("output_artifacts")
    if not isinstance(artifacts, list):
        return False
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        if o.stages.support._normalize_text(artifact.get("kind")) != "xtb_candidate":
            continue
        if o.stages.support._normalize_text(artifact.get("path")):
            return True
    return False


def _empty_xtb_handoff() -> dict[str, str]:
    return {
        "status": "",
        "reason": "",
        "message": "",
        "artifact_path": "",
    }


__all__ = [
    "stage_has_xtb_candidates_impl",
    "xtb_handoff_status_impl",
]
