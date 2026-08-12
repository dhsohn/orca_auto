from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.config.files import shared_workflow_root_from_config
from orca_auto.core.engine_catalog import (
    EngineCatalogEntry,
    find_engine_catalog_entry,
    get_engine_catalog_entry,
)
from orca_auto.core.engines import entry_matches_engine_identity
from orca_auto.core.paths.workflow import workflow_stage_dirnames_for_engine
from orca_auto.core.queue import list_queue
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.utils import normalize_text

from ..engine_runtime import engine_runtime_paths
from ..state import (
    iter_workflow_runtime_workspaces,
    workflow_workspace_internal_engine_paths,
)
from . import _orca
from ._model import (
    ActivityListRequest,
    ActivityRecord,
    ResolvedActivitySources,
    path_aliases,
    timestamp_metadata,
    unique_texts,
)


def queue_entry_status(entry: QueueEntry) -> str:
    status = normalize_text(
        getattr(getattr(entry, "status", None), "value", None)
    ) or normalize_text(getattr(entry, "status", None))
    status = status or "unknown"
    if getattr(entry, "cancel_requested", False) and status == "running":
        return "cancel_requested"
    return status


def runtime_paths_for_engine(
    config_path: str,
    *,
    engine: str,
) -> dict[str, Path]:
    return engine_runtime_paths(config_path, engine=engine)


def engine_queue_roots(
    config_path: str,
    *,
    engine: str,
) -> tuple[Path, ...]:
    runtime_paths = runtime_paths_for_engine(config_path, engine=engine)
    entry = find_engine_catalog_entry(engine)
    if entry is None or entry.workflow_stage_role != "workflow-stage":
        engine_roots: list[Path] = [runtime_paths["allowed_root"]]
        return tuple(engine_roots)

    workflow_root = shared_workflow_root_from_config(config_path)
    if not workflow_root:
        return (runtime_paths["allowed_root"],)

    roots: list[Path] = []

    for workspace_dir in iter_workflow_runtime_workspaces(workflow_root, engine=engine):
        for stage_dirname in workflow_stage_dirnames_for_engine(engine):
            runtime_paths = workflow_workspace_internal_engine_paths(
                workspace_dir,
                engine=engine,
                stage_dirname=stage_dirname,
            )
            runtime_root = runtime_paths["allowed_root"]
            if not runtime_root.exists():
                continue
            if runtime_root not in roots:
                roots.append(runtime_root)
    return tuple(roots)


def _queue_record_label(entry: QueueEntry, metadata: dict[str, Any], path_text: str) -> str:
    return (
        normalize_text(metadata.get("reaction_key"))
        or normalize_text(metadata.get("molecule_key"))
        or normalize_text(Path(path_text).name if path_text else "")
        or normalize_text(entry.task_id)
        or normalize_text(entry.queue_id)
    )


def _queue_record_aliases(
    entry: QueueEntry,
    path_text: str,
    *,
    allowed_root: Path,
) -> tuple[str, ...]:
    return unique_texts(
        [
            normalize_text(entry.queue_id),
            normalize_text(entry.task_id),
            *list(path_aliases(path_text, root=allowed_root)),
        ]
    )


def _engine_queue_record(
    entry: QueueEntry,
    *,
    app_name: str,
    engine: str,
    allowed_root: Path,
) -> ActivityRecord:
    metadata = dict(entry.metadata)
    workflow_id = normalize_text(metadata.get("workflow_id"))
    path_text = normalize_text(metadata.get("job_dir")) or normalize_text(
        metadata.get("reaction_dir")
    )
    enqueued_at = normalize_text(entry.enqueued_at)
    started_at = normalize_text(entry.started_at)
    finished_at = normalize_text(entry.finished_at)
    updated_at = finished_at or started_at or enqueued_at
    repair_blocked_reason = normalize_text(metadata.get("terminal_repair_blocked_reason"))
    return ActivityRecord(
        activity_id=normalize_text(entry.queue_id) or normalize_text(entry.task_id),
        kind="job",
        engine=engine,
        status="repair_blocked" if repair_blocked_reason else queue_entry_status(entry),
        label=_queue_record_label(entry, metadata, path_text),
        source=app_name,
        submitted_at=enqueued_at,
        updated_at=updated_at,
        cancel_target=normalize_text(entry.queue_id),
        aliases=_queue_record_aliases(
            entry,
            path_text,
            allowed_root=allowed_root,
        ),
        metadata={
            "queue_id": normalize_text(entry.queue_id),
            "task_id": normalize_text(entry.task_id),
            "task_kind": normalize_text(entry.task_kind),
            "mode": normalize_text(metadata.get("mode")),
            "job_type": normalize_text(metadata.get("job_type")),
            "workflow_id": workflow_id,
            "job_dir": path_text,
            "allowed_root": str(allowed_root),
            "priority": int(entry.priority),
            "repair_blocked_reason": repair_blocked_reason,
            "queue_error": normalize_text(getattr(entry, "error", "")),
            **timestamp_metadata(
                enqueued_at=enqueued_at,
                started_at=started_at,
                finished_at=finished_at,
            ),
        },
    )


def engine_queue_records(
    *,
    app_name: str,
    engine: str,
    config_path: str,
) -> list[ActivityRecord]:
    rows: list[ActivityRecord] = []
    for allowed_root in engine_queue_roots(config_path, engine=engine):
        for entry in list_queue(allowed_root):
            if not entry_matches_engine_identity(entry, engine):
                continue
            rows.append(
                _engine_queue_record(
                    entry,
                    app_name=app_name,
                    engine=engine,
                    allowed_root=allowed_root,
                )
            )
    return rows


def requested_child_engines(request: ActivityListRequest) -> tuple[bool, set[str]]:
    include_children = {
        normalize_text(engine).lower()
        for engine in (request.child_job_engines or ())
        if normalize_text(engine)
    }
    return request.child_job_engines is None, include_children


def collect_child_queue_activity(
    resolved: ResolvedActivitySources,
    request: ActivityListRequest,
    *,
    app_name: str,
    engine: str,
    config_path: str | None,
) -> list[ActivityRecord]:
    include_all_children, include_children = requested_child_engines(request)
    if (
        not normalize_text(resolved.workflow_root)
        or not normalize_text(config_path)
        or (not include_all_children and engine not in include_children)
    ):
        return []
    return engine_queue_records(
        app_name=app_name,
        engine=engine,
        config_path=str(config_path),
    )


def collect_crest_activity(
    resolved: ResolvedActivitySources,
    request: ActivityListRequest,
) -> list[ActivityRecord]:
    return collect_catalog_engine_activity(get_engine_catalog_entry("crest"), resolved, request)


def collect_xtb_activity(
    resolved: ResolvedActivitySources,
    request: ActivityListRequest,
) -> list[ActivityRecord]:
    return collect_catalog_engine_activity(get_engine_catalog_entry("xtb"), resolved, request)


def collect_orca_activity(
    resolved: ResolvedActivitySources,
    request: ActivityListRequest,
) -> list[ActivityRecord]:
    del request
    config_path = normalize_text(resolved.config_for_engine("orca"))
    if not config_path:
        return []
    return _orca.orca_records(
        config_path=config_path,
    )


def collect_catalog_engine_activity(
    entry: EngineCatalogEntry,
    resolved: ResolvedActivitySources,
    request: ActivityListRequest,
) -> list[ActivityRecord]:
    if entry.activity_role == "orca-run":
        return collect_orca_activity(resolved, request)
    if entry.workflow_stage_role == "workflow-stage":
        return collect_child_queue_activity(
            resolved,
            request,
            app_name=entry.source_id,
            engine=entry.engine_id,
            config_path=resolved.config_for_engine(entry.engine_id),
        )
    raise ValueError(
        f"Unsupported catalog activity role for {entry.engine_id}: {entry.activity_role}"
    )


__all__ = [
    "_engine_queue_record",
    "_queue_record_aliases",
    "_queue_record_label",
    "collect_child_queue_activity",
    "collect_catalog_engine_activity",
    "collect_crest_activity",
    "collect_orca_activity",
    "collect_xtb_activity",
    "engine_queue_records",
    "engine_queue_roots",
    "queue_entry_status",
    "requested_child_engines",
    "runtime_paths_for_engine",
]
