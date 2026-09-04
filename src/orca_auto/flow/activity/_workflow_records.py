from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.utils import normalize_text, safe_int

from ..registry import list_workflow_registry, reindex_workflow_registry
from ..state import list_workflow_summaries
from ..workflow.status import select_current_stage
from . import _sources
from ._model import ActivityRecord, mapping_text, unique_texts


def workflow_elapsed_metadata(
    *,
    record_metadata: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    restart_summary = _sources.coerce_mapping(
        record_metadata.get("restart_summary")
    ) or _sources.coerce_mapping(summary.get("restart_summary"))
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


def workflow_cancel_transitions_metadata(
    *,
    record_metadata: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Report cancel transitions no worker has journaled yet.

    Such a row cannot be cleared until the drain runs, and nothing else on the
    operator surfaces says why. The terminal clear guard decides that refusal
    from the workflow payload alone, never from the registry row, so this count
    follows the same authority: whenever a payload summary was read for the
    row, its count wins outright -- including the zero a completed drain leaves
    behind. Taking the larger of the two instead would report a refusal that no
    longer happens, because the drain rewrites only ``workflow.json`` and a
    terminal workflow is then skipped without resynchronizing the registry, so
    the row's cached count stays positive until something else reindexes it.

    An empty ``summary`` means no payload was read for this row at all:
    ``list_workflow_summaries`` skips a workspace whose ``workflow.json`` is
    missing, unreadable or unparsable, and an identity-quarantined row is filed
    under the payload's persisted id rather than this record's id. The cached
    count is the only evidence left in that state, so it is used there.

    Two states can still disagree with the clear guard, both of them already
    anomalous and neither of them the stale-cache loop above. A workspace whose
    payload is gone is cleared by the guard while a stale cached count still
    prints the note (the row and its note disappear together with that clear).
    A payload that is unreadable, or whose identity does not match the row, is
    refused by the guard on that ground rather than on this one, and the cached
    count reported here may be stale in either direction.
    """
    if summary:
        pending = safe_int(summary.get("cancel_transitions_pending"), default=0)
    else:
        pending = safe_int(record_metadata.get("cancel_transitions_pending"), default=0)
    return {"cancel_transitions_pending": pending} if pending > 0 else {}


def _workflow_summary_by_id(root: Path) -> dict[str, dict[str, Any]]:
    return {
        normalize_text(summary.get("workflow_id")): summary
        for summary in list_workflow_summaries(root)
        if normalize_text(summary.get("workflow_id"))
    }


def _workflow_record_label(
    record: Any,
    *,
    workflow_id: str,
    current_stage: dict[str, Any],
) -> str:
    return (
        mapping_text(current_stage, "reaction_dir")
        or normalize_text(record.reaction_key)
        or normalize_text(record.source_job_id)
        or normalize_text(record.template_name)
        or workflow_id
    )


def _workspace_display_name(workspace_dir: str, *, workflow_root: Path) -> str:
    """Human-facing workspace name for queue views.

    Generation-named workspaces carry the workflow id as their directory name,
    so a workspace inside a scaffold displays the scaffold directory (the name
    the user submitted, e.g. ``TS8_wf``). Direct-API workspaces sitting right
    under the workflow root keep their generation name — there is no scaffold
    to show.
    """
    if not workspace_dir:
        return ""
    workspace = Path(workspace_dir)
    name = workspace.name
    if not is_visible_generation_name(name):
        return name
    parent = workspace.parent
    try:
        parent_is_root = parent == workflow_root or parent.resolve() == workflow_root
    except OSError:
        parent_is_root = parent == workflow_root
    if not parent_is_root and parent.name:
        return parent.name
    return name


def _workflow_record_aliases(record: Any, workflow_id: str) -> tuple[str, ...]:
    workspace_dir = normalize_text(record.workspace_dir)
    return unique_texts(
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
    workflow_root: Path,
) -> ActivityRecord:
    workflow_id = normalize_text(record.workflow_id)
    record_metadata = _sources.coerce_mapping(getattr(record, "metadata", {}))
    current_stage = select_current_stage(summary.get("stage_summaries") or [])
    current_engine = mapping_text(current_stage, "engine") or "workflow"
    current_stage_id = mapping_text(current_stage, "stage_id")
    return ActivityRecord(
        activity_id=workflow_id,
        kind="workflow",
        engine="workflow",
        status=normalize_text(record.status) or "unknown",
        label=_workflow_record_label(
            record,
            workflow_id=workflow_id,
            current_stage=current_stage,
        ),
        source="orca_auto_flow",
        submitted_at=normalize_text(record.requested_at),
        updated_at=normalize_text(record.updated_at) or normalize_text(record.requested_at),
        cancel_target=workflow_id,
        aliases=_workflow_record_aliases(record, workflow_id),
        metadata={
            "template_name": normalize_text(record.template_name),
            "request_parameters": _sources.coerce_mapping(summary.get("request_parameters")),
            "workspace_dir": normalize_text(record.workspace_dir),
            "workspace_display_name": _workspace_display_name(
                normalize_text(record.workspace_dir),
                workflow_root=workflow_root,
            ),
            "workflow_file": normalize_text(record.workflow_file),
            "stage_count": int(record.stage_count),
            "reaction_key": normalize_text(record.reaction_key),
            "source_job_id": normalize_text(record.source_job_id),
            "source_job_type": normalize_text(record.source_job_type),
            "current_engine": current_engine,
            "current_stage_id": current_stage_id,
            "current_stage_status": mapping_text(current_stage, "status"),
            "current_task_status": mapping_text(current_stage, "task_status"),
            **workflow_elapsed_metadata(
                record_metadata=record_metadata,
                summary=summary,
            ),
            **workflow_cancel_transitions_metadata(
                record_metadata=record_metadata,
                summary=summary,
            ),
        },
    )


def workflow_records(
    *,
    workflow_root: str | Path,
    refresh: bool,
) -> list[ActivityRecord]:
    root = Path(workflow_root).expanduser().resolve()
    registry_records = reindex_workflow_registry(root) if refresh else list_workflow_registry(root)
    summary_by_id = _workflow_summary_by_id(root)

    rows: list[ActivityRecord] = []
    for record in registry_records:
        workflow_id = normalize_text(record.workflow_id)
        rows.append(
            _workflow_activity_record(
                record,
                summary=summary_by_id.get(workflow_id, {}),
                workflow_root=root,
            )
        )
    return rows


__all__ = [
    "_workflow_activity_record",
    "_workflow_record_aliases",
    "_workflow_record_label",
    "_workflow_summary_by_id",
    "_workspace_display_name",
    "workflow_cancel_transitions_metadata",
    "workflow_elapsed_metadata",
    "workflow_records",
]
