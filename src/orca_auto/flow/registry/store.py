from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from orca_auto.core.paths.workflow import (
    is_workflow_workspace_location,
    iter_workflow_workspace_candidate_dirs,
    validate_workflow_workspace_identity,
)
from orca_auto.core.utils import (
    atomic_write_json,
    fsync_directory,
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
from orca_auto.core.utils.lock import file_lock
from orca_auto.flow.workflow.store import WORKFLOW_CREATION_MARKER_FILE

from ..state import (
    acquire_workflow_lock,
    iter_workflow_workspaces,
    load_workflow_payload,
    workflow_summary,
)
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
    updated_at = now_utc_iso()
    metadata = {
        "downstream_reaction_workflow": _coerce_mapping(
            summary.get("downstream_reaction_workflow")
        ),
        "precomplex_handoff": _coerce_mapping(summary.get("precomplex_handoff")),
        "parent_workflow": _coerce_mapping(summary.get("parent_workflow")),
        "final_child_sync_pending": bool(summary.get("final_child_sync_pending")),
    }
    if bool(summary.get("si_publish_pending")):
        metadata["si_publish_pending"] = True
    if bool(summary.get("si_publish_blocked")):
        metadata["si_publish_blocked"] = True
    last_restarted_at = _normalize_text(summary.get("last_restarted_at"))
    if last_restarted_at:
        metadata["last_restarted_at"] = last_restarted_at
    restart_summary = _coerce_mapping(summary.get("restart_summary"))
    if restart_summary:
        metadata["restart_summary"] = restart_summary
    for key in (
        "si_publish_generation",
        "si_published_generation",
        "si_publish_error",
    ):
        value = _normalize_text(summary.get(key))
        if value:
            metadata[key] = value
    si_publish_attempts = _safe_int(summary.get("si_publish_attempts"), default=0)
    if si_publish_attempts > 0:
        metadata["si_publish_attempts"] = si_publish_attempts
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
        metadata=metadata,
    )


def _record_for_workspace(
    workspace_dir: str | Path,
    payload: dict[str, Any],
) -> WorkflowRegistryRecord:
    workspace = Path(workspace_dir).expanduser().resolve()
    indexed_payload = payload
    metadata = payload.get("metadata")
    workflow_error = metadata.get("workflow_error") if isinstance(metadata, dict) else None
    persisted_id = _normalize_text(payload.get("workflow_id"))
    try:
        validate_workflow_workspace_identity(workspace, payload.get("workflow_id"))
    except (OSError, TypeError, ValueError):
        identity_valid = False
    else:
        identity_valid = True
    if not identity_valid:
        # Keep the durable payload untouched while the registry remains
        # addressable by its trusted workspace identity.
        quarantined = (
            _normalize_text(payload.get("status")).lower() == "failed"
            and isinstance(workflow_error, dict)
            and workflow_error.get("scope") == "workflow_identity_validation"
        )
        indexed_payload = dict(payload)
        indexed_payload["workflow_id"] = workspace.name
    else:
        quarantined = False
    record = _record_from_summary(workflow_summary(workspace, indexed_payload))
    if identity_valid or not quarantined:
        return record
    return replace(
        record,
        metadata={
            **record.metadata,
            "identity_quarantined": True,
            "quarantined_persisted_workflow_id": persisted_id,
        },
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
    try:
        record_workspace = Path(record.workspace_dir).expanduser().resolve()
    except OSError:
        record_workspace = None
    record_workspace_trusted = record_workspace is not None and is_workflow_workspace_location(
        record_workspace, resolved_root
    )
    if (
        record_workspace is not None
        and record_workspace_trusted
        and record_workspace.name != record.workflow_id
    ):
        raise ValueError(
            f"workflow registry id {record.workflow_id!r} does not match workspace "
            f"name {record_workspace.name!r}"
        )
    trusted_workspace_key = (
        str(record_workspace)
        if record_workspace is not None
        and record_workspace_trusted
        and record_workspace.name == record.workflow_id
        else ""
    )
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

        records = [
            existing
            for existing in records
            if existing.workflow_id != record.workflow_id
            and not (
                trusted_workspace_key
                and _markers.normal_path_key(existing.workspace_dir) == trusted_workspace_key
            )
        ]
        records.append(record)
        records.sort(key=lambda item: (item.requested_at, item.workflow_id), reverse=True)
        _save_records(resolved_root, records)
    return record


def sync_workflow_registry(
    workflow_root: str | Path, workspace_dir: str | Path, payload: dict[str, Any] | None = None
) -> WorkflowRegistryRecord:
    current = payload if payload is not None else load_workflow_payload(workspace_dir)
    record = _record_for_workspace(workspace_dir, current)
    return upsert_workflow_registry_record(workflow_root, record)


def reindex_workflow_registry(workflow_root: str | Path) -> list[WorkflowRegistryRecord]:
    root = Path(workflow_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    records: list[WorkflowRegistryRecord] = []
    for workspace_dir in iter_workflow_workspaces(root):
        try:
            payload = load_workflow_payload(workspace_dir)
            record = _record_for_workspace(workspace_dir, payload)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            continue
        records.append(record)
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
    published_creation_markers = [
        workspace / WORKFLOW_CREATION_MARKER_FILE
        for workspace in iter_workflow_workspace_candidate_dirs(resolved_root)
        if (workspace / "workflow.json").is_file()
        and (workspace / WORKFLOW_CREATION_MARKER_FILE).is_file()
    ]
    if published_creation_markers:
        repaired_records = (reindex_fn or reindex_workflow_registry)(resolved_root)
        repaired_workspaces = {
            Path(record.workspace_dir).expanduser().resolve() for record in repaired_records
        }
        for marker in published_creation_markers:
            if marker.parent.resolve() not in repaired_workspaces:
                continue
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
            else:
                fsync_directory(marker.parent)
        return repaired_records
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

    # Snapshot under the registry lock, then release it before attempting any
    # workflow lock. All mutators use workflow -> registry lock order; reversing
    # that order here would deadlock with advance/restart/cancellation.
    with file_lock(_registry_lock_path(resolved_root)):
        candidates = [
            record
            for record in _load_records(resolved_root)
            if _record_is_clearable_terminal(record, target_statuses)
        ]

    def remove_if_still_clearable(
        candidate: WorkflowRegistryRecord, *, require_missing_payload: bool = False
    ) -> bool:
        with file_lock(_registry_lock_path(resolved_root)):
            records = _load_records(resolved_root)
            matches = [record for record in records if record.workflow_id == candidate.workflow_id]
            if len(matches) != 1:
                return False
            current = matches[0]
            if (
                current.workspace_dir != candidate.workspace_dir
                or not _record_is_clearable_terminal(current, target_statuses)
            ):
                return False
            if require_missing_payload:
                current_workspace = _normalize_text(current.workspace_dir)
                if (
                    current_workspace
                    and (Path(current_workspace).expanduser().resolve() / "workflow.json").is_file()
                ):
                    return False
            try:
                authoritative = load_workflow_payload(current.workspace_dir)
            except FileNotFoundError:
                authoritative = None
            except (OSError, ValueError, json.JSONDecodeError):
                # Corrupt/unreadable durable state is not safe to hide.
                return False
            if authoritative is not None:
                try:
                    authoritative_id = validate_workflow_workspace_identity(
                        current.workspace_dir,
                        authoritative.get("workflow_id"),
                    )
                except (OSError, TypeError, ValueError):
                    return False
                if authoritative_id != current.workflow_id:
                    return False
                if _normalize_text(authoritative.get("status")).lower() not in target_statuses:
                    # A restart/other mutator may have persisted an active state
                    # before its registry sync. Never clear its only discoverable row.
                    return False
                metadata = authoritative.get("metadata")
                if isinstance(metadata, dict) and bool(
                    metadata.get("si_publish_pending")
                    or metadata.get("si_publish_blocked")
                    or metadata.get("final_child_sync_pending")
                ):
                    return False

            markers, markers_changed = _markers.add_cleared_markers(
                _load_cleared_markers(resolved_root),
                [current],
                cleared_at=now_utc_iso(),
            )
            if markers_changed:
                _save_cleared_markers(resolved_root, markers)
            records.remove(current)
            _save_records(resolved_root, records)
            return True

    removed_count = 0
    for candidate in candidates:
        workspace_text = _normalize_text(candidate.workspace_dir)
        try:
            workspace = Path(workspace_text).expanduser().resolve() if workspace_text else None
        except OSError:
            continue
        # Registry state is not trusted to choose arbitrary lock-file targets.
        # Workflow workspaces are direct root children or generation dirs in a
        # direct-child scaffold, named by workflow_id; resolving first also
        # rejects a child symlink that escapes the root.
        if (
            workspace is None
            or not is_workflow_workspace_location(workspace, resolved_root)
            or workspace.name != candidate.workflow_id
        ):
            continue
        if not (workspace / "workflow.json").is_file():
            # There is no authoritative workflow payload to fence. Recheck absence
            # while serializing against registry writers; a later active/pending
            # upsert removes the cleared marker and safely resurrects the record.
            removed_count += int(remove_if_still_clearable(candidate, require_missing_payload=True))
            continue
        try:
            # Busy means a mutator may be between authoritative and registry
            # checkpoints. Keep the row and let a later clear retry.
            with acquire_workflow_lock(workspace, timeout_seconds=0.0):
                removed_count += int(remove_if_still_clearable(candidate))
        except (OSError, TimeoutError):
            continue
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
