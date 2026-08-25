from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.utils import (
    coerce_bool as _coerce_bool,
)
from orca_auto.core.utils import (
    coerce_list as _coerce_sequence,
)
from orca_auto.core.utils import (
    coerce_mapping as _coerce_mapping,
)
from orca_auto.core.utils import (
    normalize_text as _normalize_text,
)
from orca_auto.flow.contracts.workflow import (
    coerce_workflow_plan_payload,
    workflow_request_parameters,
)

from .store import iter_workflow_workspaces, load_workflow_payload, workflow_file_path


def workflow_has_active_downstream(payload: dict[str, Any]) -> bool:
    metadata = _coerce_mapping(payload.get("metadata"))
    downstream = _coerce_mapping(metadata.get("downstream_reaction_workflow"))
    status = _normalize_text(downstream.get("status")).lower()
    if status in {"planned", "queued", "running", "submitted", "cancel_requested"}:
        return True
    if _coerce_bool(downstream.get("final_child_sync_pending")):
        return True
    latest_stage = _coerce_mapping(downstream.get("latest_stage"))
    if _normalize_text(latest_stage.get("status")).lower() in {
        "planned",
        "queued",
        "running",
        "submitted",
        "cancel_requested",
    }:
        return True
    if _normalize_text(latest_stage.get("task_status")).lower() in {
        "planned",
        "queued",
        "running",
        "submitted",
        "cancel_requested",
    }:
        return True
    return False


def _workflow_stage_summary(stage: dict[str, Any]) -> dict[str, Any]:
    stage_status = _normalize_text(stage.get("status")) or "unknown"
    task = _coerce_mapping(stage.get("task"))
    task_status = _normalize_text(task.get("status")) or "unknown"
    task_payload = _coerce_mapping(task.get("payload"))
    enqueue_payload = _coerce_mapping(task.get("enqueue_payload"))
    submission_result = _coerce_mapping(task.get("submission_result"))
    stage_metadata = _coerce_mapping(stage.get("metadata"))
    return {
        "stage_id": _normalize_text(stage.get("stage_id")),
        "stage_kind": _normalize_text(stage.get("stage_kind")),
        "status": stage_status,
        "task_status": task_status,
        "engine": _normalize_text(task.get("engine")),
        "task_kind": _normalize_text(task.get("task_kind")),
        "input_role": _normalize_text(
            stage_metadata.get("input_role") or task_payload.get("input_role")
        ),
        "reaction_key": _normalize_text(
            task_payload.get("reaction_key") or enqueue_payload.get("reaction_key")
        ),
        "queue_id": _normalize_text(stage_metadata.get("queue_id")),
        "reaction_dir": _normalize_text(
            task_payload.get("reaction_dir") or enqueue_payload.get("reaction_dir")
        ),
        "selected_input_xyz": _normalize_text(task_payload.get("selected_input_xyz")),
        "selected_inp": _normalize_text(
            task_payload.get("selected_inp") or enqueue_payload.get("selected_inp")
        ),
        "submission_status": _normalize_text(submission_result.get("status")),
        "submission_error_detail": _normalize_text(stage_metadata.get("submission_error_detail")),
        "run_id": _normalize_text(stage_metadata.get("run_id")),
        "latest_known_path": _normalize_text(stage_metadata.get("latest_known_path")),
        "optimized_xyz_path": _normalize_text(
            stage_metadata.get("optimized_xyz_path") or task_payload.get("optimized_xyz_path")
        ),
        "analyzer_status": _normalize_text(stage_metadata.get("analyzer_status")),
        "reason": _normalize_text(stage_metadata.get("reason")),
        "reaction_handoff_status": _normalize_text(stage_metadata.get("reaction_handoff_status")),
        "reaction_handoff_reason": _normalize_text(stage_metadata.get("reaction_handoff_reason")),
        "xtb_handoff_retries_used": stage_metadata.get("xtb_handoff_retries_used"),
        "xtb_handoff_retry_limit": stage_metadata.get("xtb_handoff_retry_limit"),
        "orca_attempt_count": stage_metadata.get("attempt_count"),
        "orca_max_retries": stage_metadata.get("max_retries"),
        "completed_at": _normalize_text(stage_metadata.get("completed_at")),
        "output_artifact_count": len(_coerce_sequence(stage.get("output_artifacts"))),
        "last_out_path": _normalize_text(task_payload.get("last_out_path")),
    }


def _workflow_stage_summary_rows(
    stages: list[Any],
) -> tuple[dict[str, int], dict[str, int], list[dict[str, Any]]]:
    status_counts: dict[str, int] = {}
    task_status_counts: dict[str, int] = {}
    stage_summaries: list[dict[str, Any]] = []

    for raw_stage in stages:
        stage = _coerce_mapping(raw_stage)
        summary = _workflow_stage_summary(stage)
        status_counts[summary["status"]] = status_counts.get(summary["status"], 0) + 1
        task_status = summary["task_status"]
        task_status_counts[task_status] = task_status_counts.get(task_status, 0) + 1
        stage_summaries.append(summary)
    return status_counts, task_status_counts, stage_summaries


def workflow_summary(
    workspace_dir: str | Path, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    workspace = Path(workspace_dir).expanduser().resolve()
    data = coerce_workflow_plan_payload(
        payload if payload is not None else load_workflow_payload(workspace)
    )
    stages = _coerce_sequence(data.get("stages"))
    status_counts, task_status_counts, stage_summaries = _workflow_stage_summary_rows(stages)

    metadata = _coerce_mapping(data.get("metadata"))
    downstream = _coerce_mapping(metadata.get("downstream_reaction_workflow"))
    precomplex_handoff = _coerce_mapping(metadata.get("precomplex_handoff"))
    parent_workflow = _coerce_mapping(metadata.get("parent_workflow"))
    summary = {
        "workflow_id": _normalize_text(data.get("workflow_id")),
        "template_name": _normalize_text(data.get("template_name")),
        "status": _normalize_text(data.get("status")),
        "source_job_id": _normalize_text(data.get("source_job_id")),
        "source_job_type": _normalize_text(data.get("source_job_type")),
        "reaction_key": _normalize_text(data.get("reaction_key")),
        "requested_at": _normalize_text(data.get("requested_at")),
        "workspace_dir": str(workspace),
        "workflow_file": str(workflow_file_path(workspace)),
        "stage_count": len(stages),
        "stage_status_counts": status_counts,
        "task_status_counts": task_status_counts,
        "request_parameters": workflow_request_parameters(data),
        "downstream_reaction_workflow": downstream,
        "precomplex_handoff": precomplex_handoff,
        "parent_workflow": parent_workflow,
        "final_child_sync_pending": _coerce_bool(metadata.get("final_child_sync_pending")),
        "si_publish_pending": _coerce_bool(metadata.get("si_publish_pending")),
        "si_publish_blocked": _coerce_bool(metadata.get("si_publish_blocked")),
        "stage_summaries": stage_summaries,
    }
    last_restarted_at = _normalize_text(metadata.get("last_restarted_at"))
    if last_restarted_at:
        summary["last_restarted_at"] = last_restarted_at
    restart_summary = _coerce_mapping(metadata.get("restart_summary"))
    if restart_summary:
        summary["restart_summary"] = restart_summary
    si_publish_generation = _normalize_text(metadata.get("si_publish_generation"))
    if si_publish_generation:
        summary["si_publish_generation"] = si_publish_generation
    si_published_generation = _normalize_text(metadata.get("si_published_generation"))
    if si_published_generation:
        summary["si_published_generation"] = si_published_generation
    si_publish_error = _normalize_text(metadata.get("si_publish_error"))
    if si_publish_error:
        summary["si_publish_error"] = si_publish_error
    try:
        si_publish_attempts = max(0, int(metadata.get("si_publish_attempts", 0)))
    except (TypeError, ValueError):
        si_publish_attempts = 0
    if si_publish_attempts:
        summary["si_publish_attempts"] = si_publish_attempts
    return summary


def list_workflow_summaries(workflow_root: str | Path) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for workspace in iter_workflow_workspaces(workflow_root):
        try:
            summaries.append(workflow_summary(workspace))
        except (FileNotFoundError, ValueError):
            continue
    return summaries


__all__ = [
    "list_workflow_summaries",
    "workflow_has_active_downstream",
    "workflow_summary",
]
