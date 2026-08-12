from __future__ import annotations

from typing import TYPE_CHECKING, Any

from orca_auto.core.utils.coercion import normalize_text, safe_int

if TYPE_CHECKING:
    from .runtime.models import StageTransitionContext


def stage_key(stage: dict[str, Any], index: int) -> str:
    stage_id = normalize_text(stage.get("stage_id"))
    if stage_id:
        return stage_id
    return f"index:{index}"


def stage_event_metadata(stage: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    text_fields = (
        "stage_id",
        "stage_kind",
        "engine",
        "task_kind",
        "task_status",
        "queue_id",
        "reaction_dir",
        "selected_input_xyz",
        "selected_inp",
        "submission_status",
        "submission_error_detail",
        "run_id",
        "latest_known_path",
        "optimized_xyz_path",
        "analyzer_status",
        "reason",
        "reaction_handoff_status",
        "reaction_handoff_reason",
        "completed_at",
        "last_out_path",
    )
    int_fields = (
        "xtb_handoff_retries_used",
        "xtb_handoff_retry_limit",
        "orca_attempt_count",
        "orca_max_retries",
        "output_artifact_count",
    )
    for field in text_fields:
        text = normalize_text(stage.get(field))
        if text:
            metadata[field] = text
    for field in int_fields:
        value = safe_int(stage.get(field), default=None)
        if value is not None:
            metadata[field] = value
    return metadata


def stage_status_event_type(
    previous_stage: dict[str, Any],
    current_stage: dict[str, Any],
    *,
    suppress_terminal_event: bool,
) -> str:
    previous_status = normalize_text(previous_stage.get("status")).lower()
    current_status = normalize_text(current_stage.get("status")).lower()
    if not current_status or current_status == previous_status:
        return ""
    if current_status == "queued":
        return "workflow_stage_submitted"
    if current_status in {"submitted", "running"}:
        return "workflow_stage_status_changed"
    if suppress_terminal_event:
        return ""
    if current_status == "completed":
        return "workflow_stage_completed"
    if current_status in {"failed", "submission_failed", "cancel_failed"}:
        return "workflow_stage_failed"
    if current_status == "cancelled":
        return "workflow_stage_cancelled"
    return ""


def stage_handoff_event_type(previous_stage: dict[str, Any], current_stage: dict[str, Any]) -> str:
    engine = normalize_text(current_stage.get("engine") or previous_stage.get("engine")).lower()
    task_kind = normalize_text(
        current_stage.get("task_kind") or previous_stage.get("task_kind")
    ).lower()
    if engine != "xtb" or task_kind != "path_search":
        return ""
    previous_handoff = normalize_text(previous_stage.get("reaction_handoff_status")).lower()
    current_handoff = normalize_text(current_stage.get("reaction_handoff_status")).lower()
    if not current_handoff or current_handoff == previous_handoff:
        return ""
    if current_handoff == "ready":
        return "workflow_stage_handoff_ready"
    if current_handoff == "retrying":
        return "workflow_stage_handoff_retrying"
    if current_handoff == "failed":
        return "workflow_stage_handoff_failed"
    return ""


def stage_transition_context(
    previous_stage: dict[str, Any],
    current_stage: dict[str, Any],
) -> StageTransitionContext:
    return {
        "previous_stage_status": normalize_text(previous_stage.get("status")).lower(),
        "current_stage_status": normalize_text(current_stage.get("status")).lower(),
        "previous_handoff_status": normalize_text(
            previous_stage.get("reaction_handoff_status")
        ).lower(),
        "current_handoff_status": normalize_text(
            current_stage.get("reaction_handoff_status")
        ).lower(),
        "stage_id": normalize_text(current_stage.get("stage_id") or previous_stage.get("stage_id")),
        "engine": normalize_text(current_stage.get("engine") or previous_stage.get("engine")),
        "task_kind": normalize_text(
            current_stage.get("task_kind") or previous_stage.get("task_kind")
        ),
    }


def stage_transition_metadata(
    metadata: dict[str, Any],
    context: StageTransitionContext,
    *,
    include_handoff: bool,
) -> dict[str, Any]:
    event_metadata = dict(metadata)
    if context["previous_stage_status"]:
        event_metadata["previous_stage_status"] = context["previous_stage_status"]
    if context["current_stage_status"]:
        event_metadata["stage_status"] = context["current_stage_status"]
    if include_handoff and context["previous_handoff_status"]:
        event_metadata["previous_reaction_handoff_status"] = context["previous_handoff_status"]
    if include_handoff and context["current_handoff_status"]:
        event_metadata["reaction_handoff_status"] = context["current_handoff_status"]
    return event_metadata


__all__ = [
    "stage_event_metadata",
    "stage_handoff_event_type",
    "stage_key",
    "stage_status_event_type",
    "stage_transition_context",
    "stage_transition_metadata",
]
