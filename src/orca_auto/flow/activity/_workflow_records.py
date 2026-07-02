from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.utils import normalize_text

from ._list_deps import ActivityListDeps
from ._model import ActivityRecord


def workflow_elapsed_metadata(
    *,
    record_metadata: dict[str, Any],
    summary: dict[str, Any],
    deps: ActivityListDeps,
) -> dict[str, Any]:
    restart_summary = deps._coerce_mapping(
        record_metadata.get("restart_summary")
    ) or deps._coerce_mapping(summary.get("restart_summary"))
    last_restarted_at = (
        normalize_text(record_metadata.get("last_restarted_at"))
        or normalize_text(summary.get("last_restarted_at"))
        or normalize_text(restart_summary.get("restarted_at"))
    )
    metadata: dict[str, Any] = {}
    if last_restarted_at:
        metadata["last_restarted_at"] = last_restarted_at
        metadata["elapsed_started_at"] = last_restarted_at
    if restart_summary:
        metadata["restart_summary"] = restart_summary
    return metadata


def _workflow_summary_by_id(root: Path, deps: ActivityListDeps) -> dict[str, dict[str, Any]]:
    return {
        normalize_text(summary.get("workflow_id")): summary
        for summary in deps.list_workflow_summaries(root)
        if normalize_text(summary.get("workflow_id"))
    }


def _workflow_record_label(
    record: Any,
    *,
    workflow_id: str,
    current_stage: dict[str, Any],
    deps: ActivityListDeps,
) -> str:
    return (
        deps._mapping_text(current_stage, "reaction_dir")
        or normalize_text(record.reaction_key)
        or normalize_text(record.source_job_id)
        or normalize_text(record.template_name)
        or workflow_id
    )


def _workflow_record_aliases(
    record: Any, workflow_id: str, deps: ActivityListDeps
) -> tuple[str, ...]:
    workspace_dir = normalize_text(record.workspace_dir)
    return deps._unique_texts(
        [
            workflow_id,
            workspace_dir,
            normalize_text(record.workflow_file),
            Path(workspace_dir).name if workspace_dir else "",
        ]
    )


def _workflow_activity_record(
    record: Any,
    *,
    summary: dict[str, Any],
    deps: ActivityListDeps,
) -> ActivityRecord:
    workflow_id = normalize_text(record.workflow_id)
    record_metadata = deps._coerce_mapping(getattr(record, "metadata", {}))
    current_stage = deps.select_current_stage(summary.get("stage_summaries") or [])
    current_engine = deps._mapping_text(current_stage, "engine") or "workflow"
    current_stage_id = deps._mapping_text(current_stage, "stage_id")
    return ActivityRecord(
        activity_id=workflow_id,
        kind="workflow",
        engine="workflow",
        status=normalize_text(record.status) or "unknown",
        label=_workflow_record_label(
            record,
            workflow_id=workflow_id,
            current_stage=current_stage,
            deps=deps,
        ),
        source="orca_auto_flow",
        submitted_at=normalize_text(record.requested_at),
        updated_at=normalize_text(record.updated_at) or normalize_text(record.requested_at),
        cancel_target=workflow_id,
        aliases=_workflow_record_aliases(record, workflow_id, deps),
        metadata={
            "template_name": normalize_text(record.template_name),
            "request_parameters": deps._coerce_mapping(summary.get("request_parameters")),
            "workspace_dir": normalize_text(record.workspace_dir),
            "workflow_file": normalize_text(record.workflow_file),
            "stage_count": int(record.stage_count),
            "reaction_key": normalize_text(record.reaction_key),
            "source_job_id": normalize_text(record.source_job_id),
            "source_job_type": normalize_text(record.source_job_type),
            "current_engine": current_engine,
            "current_stage_id": current_stage_id,
            "current_stage_status": deps._mapping_text(current_stage, "status"),
            "current_task_status": deps._mapping_text(current_stage, "task_status"),
            **workflow_elapsed_metadata(
                record_metadata=record_metadata,
                summary=summary,
                deps=deps,
            ),
        },
    )


def workflow_records(
    *,
    workflow_root: str | Path,
    refresh: bool,
    deps: ActivityListDeps,
) -> list[ActivityRecord]:
    root = Path(workflow_root).expanduser().resolve()
    registry_records = (
        deps.reindex_workflow_registry(root) if refresh else deps.list_workflow_registry(root)
    )
    summary_by_id = _workflow_summary_by_id(root, deps)

    rows: list[ActivityRecord] = []
    for record in registry_records:
        workflow_id = normalize_text(record.workflow_id)
        rows.append(
            _workflow_activity_record(
                record,
                summary=summary_by_id.get(workflow_id, {}),
                deps=deps,
            )
        )
    return rows


__all__ = [
    "_workflow_activity_record",
    "_workflow_record_aliases",
    "_workflow_record_label",
    "_workflow_summary_by_id",
    "workflow_elapsed_metadata",
    "workflow_records",
]
