from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.core.utils import (
    atomic_write_json,
    file_lock,
    now_utc_iso,
)
from orca_auto.core.utils import (
    coerce_mapping as _coerce_mapping,
)
from orca_auto.core.utils import (
    normalize_text as _normalize_text,
)
from orca_auto.core.utils import (
    safe_int as _safe_int,
)

from ..state import iter_workflow_workspaces, load_workflow_payload, workflow_summary
from . import _markers as _markers

WORKFLOW_REGISTRY_FILE_NAME = "workflow_registry.json"
WORKFLOW_REGISTRY_LOCK_NAME = "workflow_registry.lock"
WORKFLOW_REGISTRY_CLEARED_FILE_NAME = "workflow_registry_cleared.json"
_TERMINAL_WORKFLOW_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "cancel_failed", "submission_failed"}
)


class WorkflowRegistryCorruptError(RuntimeError):
    """Raised when workflow registry state exists but cannot be safely loaded."""


@dataclass(frozen=True)
class WorkflowRegistryRecord:
    workflow_id: str
    template_name: str
    status: str
    source_job_id: str
    source_job_type: str
    reaction_key: str
    requested_at: str
    workspace_dir: str
    workflow_file: str
    stage_count: int = 0
    updated_at: str = ""
    stage_status_counts: dict[str, int] = field(default_factory=dict)
    task_status_counts: dict[str, int] = field(default_factory=dict)
    submission_summary: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


def _registry_path(workflow_root: str | Path) -> Path:
    root = Path(workflow_root).expanduser().resolve()
    return root / WORKFLOW_REGISTRY_FILE_NAME


def _registry_lock_path(workflow_root: str | Path) -> Path:
    root = Path(workflow_root).expanduser().resolve()
    return root / WORKFLOW_REGISTRY_LOCK_NAME


def _cleared_path(workflow_root: str | Path) -> Path:
    root = Path(workflow_root).expanduser().resolve()
    return root / WORKFLOW_REGISTRY_CLEARED_FILE_NAME


def _read_existing_json(path: Path, *, description: str, missing_default: Any) -> Any:
    if not path.exists():
        return missing_default
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return missing_default
    except OSError as exc:
        raise WorkflowRegistryCorruptError(f"{description} cannot be read: {path}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkflowRegistryCorruptError(f"{description} is not valid JSON: {path}") from exc


def _coerce_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for key, item in value.items():
        text = _normalize_text(key)
        if not text:
            continue
        parsed = _safe_int(item, default=None)
        if parsed is None:
            continue
        counts[text] = parsed
    return counts


def _record_from_summary(summary: dict[str, Any]) -> WorkflowRegistryRecord:
    workspace_dir = _normalize_text(summary.get("workspace_dir"))
    updated_at = (
        _normalize_text(_coerce_mapping(summary.get("submission_summary")).get("updated_at"))
        or now_utc_iso()
    )
    metadata = {
        "downstream_reaction_workflow": _coerce_mapping(
            summary.get("downstream_reaction_workflow")
        ),
        "precomplex_handoff": _coerce_mapping(summary.get("precomplex_handoff")),
        "parent_workflow": _coerce_mapping(summary.get("parent_workflow")),
        "final_child_sync_pending": bool(summary.get("final_child_sync_pending")),
    }
    last_restarted_at = _normalize_text(summary.get("last_restarted_at"))
    if last_restarted_at:
        metadata["last_restarted_at"] = last_restarted_at
    restart_summary = _coerce_mapping(summary.get("restart_summary"))
    if restart_summary:
        metadata["restart_summary"] = restart_summary
    return WorkflowRegistryRecord(
        workflow_id=_normalize_text(summary.get("workflow_id")),
        template_name=_normalize_text(summary.get("template_name")),
        status=_normalize_text(summary.get("status")),
        source_job_id=_normalize_text(summary.get("source_job_id")),
        source_job_type=_normalize_text(summary.get("source_job_type")),
        reaction_key=_normalize_text(summary.get("reaction_key")),
        requested_at=_normalize_text(summary.get("requested_at")),
        workspace_dir=workspace_dir,
        workflow_file=str(Path(workspace_dir).expanduser().resolve() / "workflow.json")
        if workspace_dir
        else "",
        stage_count=int(summary.get("stage_count", 0) or 0),
        updated_at=updated_at,
        stage_status_counts=_coerce_counts(summary.get("stage_status_counts")),
        task_status_counts=_coerce_counts(summary.get("task_status_counts")),
        submission_summary=_coerce_mapping(summary.get("submission_summary")),
        metadata=metadata,
    )


def _record_to_dict(record: WorkflowRegistryRecord) -> dict[str, Any]:
    return asdict(record)


def _record_from_dict(raw: dict[str, Any]) -> WorkflowRegistryRecord:
    return WorkflowRegistryRecord(
        workflow_id=_normalize_text(raw.get("workflow_id")),
        template_name=_normalize_text(raw.get("template_name")),
        status=_normalize_text(raw.get("status")),
        source_job_id=_normalize_text(raw.get("source_job_id")),
        source_job_type=_normalize_text(raw.get("source_job_type")),
        reaction_key=_normalize_text(raw.get("reaction_key")),
        requested_at=_normalize_text(raw.get("requested_at")),
        workspace_dir=_normalize_text(raw.get("workspace_dir")),
        workflow_file=_normalize_text(raw.get("workflow_file")),
        stage_count=int(raw.get("stage_count", 0) or 0),
        updated_at=_normalize_text(raw.get("updated_at")),
        stage_status_counts=_coerce_counts(raw.get("stage_status_counts")),
        task_status_counts=_coerce_counts(raw.get("task_status_counts")),
        submission_summary=_coerce_mapping(raw.get("submission_summary")),
        metadata=_coerce_mapping(raw.get("metadata")),
    )


def _load_records(workflow_root: str | Path) -> list[WorkflowRegistryRecord]:
    path = _registry_path(workflow_root)
    raw = _read_existing_json(path, description="Workflow registry file", missing_default=[])
    if not isinstance(raw, list):
        raise WorkflowRegistryCorruptError(
            f"Workflow registry file must contain a JSON list: {path}"
        )
    return [_record_from_dict(item) for item in raw if isinstance(item, dict)]


def _save_records(workflow_root: str | Path, records: list[WorkflowRegistryRecord]) -> None:
    atomic_write_json(
        _registry_path(workflow_root),
        [_record_to_dict(record) for record in records],
        ensure_ascii=True,
        indent=2,
    )


def _load_cleared_markers(workflow_root: str | Path) -> list[dict[str, Any]]:
    path = _cleared_path(workflow_root)
    raw = _read_existing_json(path, description="Workflow cleared markers file", missing_default=[])
    if not isinstance(raw, list):
        raise WorkflowRegistryCorruptError(
            f"Workflow cleared markers file must contain a JSON list: {path}"
        )
    return [_coerce_mapping(item) for item in raw if isinstance(item, dict)]


def _save_cleared_markers(workflow_root: str | Path, markers: list[dict[str, Any]]) -> None:
    atomic_write_json(_cleared_path(workflow_root), markers, ensure_ascii=True, indent=2)


def _record_is_clearable_terminal(
    record: WorkflowRegistryRecord, statuses: set[str] | frozenset[str] | None = None
) -> bool:
    return _markers.record_is_clearable_terminal(
        record,
        statuses or _TERMINAL_WORKFLOW_STATUSES,
    )


def _filter_cleared_terminal_records(
    records: list[WorkflowRegistryRecord],
    markers: list[dict[str, Any]],
) -> list[WorkflowRegistryRecord]:
    return _markers.filter_cleared_terminal_records(
        records,
        markers,
        terminal_statuses=_TERMINAL_WORKFLOW_STATUSES,
    )


def upsert_workflow_registry_record(
    workflow_root: str | Path, record: WorkflowRegistryRecord
) -> WorkflowRegistryRecord:
    resolved_root = Path(workflow_root).expanduser().resolve()
    resolved_root.mkdir(parents=True, exist_ok=True)
    with file_lock(_registry_lock_path(resolved_root)):
        cleared_markers = _load_cleared_markers(resolved_root)
        records = _load_records(resolved_root)
        is_clearable_terminal = _record_is_clearable_terminal(record)
        matches_cleared_marker = _markers.record_matches_cleared_marker(
            record,
            cleared_markers,
        )
        if is_clearable_terminal and matches_cleared_marker:
            return record
        if not is_clearable_terminal and matches_cleared_marker:
            cleared_markers, removed_marker = _markers.remove_matching_cleared_markers(
                cleared_markers, record
            )
            if removed_marker:
                _save_cleared_markers(resolved_root, cleared_markers)

        updated = False
        for index, existing in enumerate(records):
            if existing.workflow_id != record.workflow_id:
                continue
            records[index] = record
            updated = True
            break
        if not updated:
            records.append(record)
        records.sort(key=lambda item: (item.requested_at, item.workflow_id), reverse=True)
        _save_records(resolved_root, records)
    return record


def sync_workflow_registry(
    workflow_root: str | Path, workspace_dir: str | Path, payload: dict[str, Any] | None = None
) -> WorkflowRegistryRecord:
    summary = workflow_summary(workspace_dir, payload)
    record = _record_from_summary(summary)
    return upsert_workflow_registry_record(workflow_root, record)


def reindex_workflow_registry(workflow_root: str | Path) -> list[WorkflowRegistryRecord]:
    root = Path(workflow_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    records: list[WorkflowRegistryRecord] = []
    for workspace_dir in iter_workflow_workspaces(root):
        try:
            payload = load_workflow_payload(workspace_dir)
            summary = workflow_summary(workspace_dir, payload)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            continue
        records.append(_record_from_summary(summary))
    records.sort(key=lambda item: (item.requested_at, item.workflow_id), reverse=True)
    with file_lock(_registry_lock_path(root)):
        _load_records(root)
        cleared_markers = _load_cleared_markers(root)
        markers_changed = False
        for record in records:
            if _record_is_clearable_terminal(record) or not _markers.record_matches_cleared_marker(
                record, cleared_markers
            ):
                continue
            cleared_markers, removed_marker = _markers.remove_matching_cleared_markers(
                cleared_markers, record
            )
            markers_changed = markers_changed or removed_marker
        records = _filter_cleared_terminal_records(records, cleared_markers)
        if markers_changed:
            _save_cleared_markers(root, cleared_markers)
        _save_records(root, records)
    return records


def list_workflow_registry(
    workflow_root: str | Path,
    *,
    reindex_if_missing: bool = True,
    reindex_fn: Callable[[str | Path], list[WorkflowRegistryRecord]] | None = None,
) -> list[WorkflowRegistryRecord]:
    resolved_root = Path(workflow_root).expanduser().resolve()
    resolved_root.mkdir(parents=True, exist_ok=True)
    path = _registry_path(resolved_root)
    with file_lock(_registry_lock_path(resolved_root)):
        if path.exists():
            return _load_records(resolved_root)
    records: list[WorkflowRegistryRecord] = []
    if not reindex_if_missing:
        return records
    return (reindex_fn or reindex_workflow_registry)(resolved_root)


def clear_terminal_workflow_registry(
    workflow_root: str | Path,
    *,
    statuses: set[str] | frozenset[str] | None = None,
    reindex_fn: Callable[[str | Path], list[WorkflowRegistryRecord]] | None = None,
) -> int:
    resolved_root = Path(workflow_root).expanduser().resolve()
    resolved_root.mkdir(parents=True, exist_ok=True)
    if not _registry_path(resolved_root).exists():
        (reindex_fn or reindex_workflow_registry)(resolved_root)

    target_statuses = {
        _normalize_text(status).lower()
        for status in (statuses or _TERMINAL_WORKFLOW_STATUSES)
        if _normalize_text(status)
    }
    if not target_statuses:
        return 0

    with file_lock(_registry_lock_path(resolved_root)):
        records = _load_records(resolved_root)
        removed_records = [
            record
            for record in records
            if _normalize_text(record.status).lower() in target_statuses
        ]
        kept_records = [
            record
            for record in records
            if _normalize_text(record.status).lower() not in target_statuses
        ]
        removed_count = len(records) - len(kept_records)
        if removed_count > 0:
            markers, markers_changed = _markers.add_cleared_markers(
                _load_cleared_markers(resolved_root),
                removed_records,
                cleared_at=now_utc_iso(),
            )
            if markers_changed:
                _save_cleared_markers(resolved_root, markers)
            _save_records(resolved_root, kept_records)
        return removed_count


def get_workflow_registry_record(
    workflow_root: str | Path, workflow_id: str
) -> WorkflowRegistryRecord | None:
    target = _normalize_text(workflow_id)
    if not target:
        return None
    for record in list_workflow_registry(workflow_root):
        if record.workflow_id == target:
            return record
    return None


def resolve_workflow_registry_record(
    workflow_root: str | Path, target: str
) -> WorkflowRegistryRecord | None:
    normalized = _normalize_text(target)
    if not normalized:
        return None

    direct: Path | None
    try:
        direct = Path(normalized).expanduser().resolve()
    except OSError:
        direct = None

    for record in list_workflow_registry(workflow_root):
        if record.workflow_id == normalized:
            return record
        if direct is None:
            continue
        if direct == Path(record.workspace_dir).expanduser().resolve():
            return record
        if record.workflow_file and direct == Path(record.workflow_file).expanduser().resolve():
            return record
    return None
