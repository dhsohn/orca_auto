from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.paths.workflow import workflow_stage_dirnames_for_engine
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.utils import normalize_text

from ._list_deps import ActivityListDeps
from ._model import ActivityListRequest, ActivityRecord, ResolvedActivitySources


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
    deps: ActivityListDeps,
) -> dict[str, Path]:
    return deps.engine_runtime_paths(config_path, engine=engine)


def engine_queue_roots(
    config_path: str,
    *,
    engine: str,
    deps: ActivityListDeps,
) -> tuple[Path, ...]:
    runtime_paths = runtime_paths_for_engine(config_path, engine=engine, deps=deps)
    if engine not in {"xtb", "crest"}:
        engine_roots: list[Path] = [runtime_paths["allowed_root"]]
        return tuple(engine_roots)

    workflow_root = deps.shared_workflow_root_from_config(config_path)
    if not workflow_root:
        return (runtime_paths["allowed_root"],)

    roots: list[Path] = []

    for workspace_dir in deps.iter_workflow_runtime_workspaces(workflow_root, engine=engine):
        for stage_dirname in workflow_stage_dirnames_for_engine(engine):
            runtime_paths = deps.workflow_workspace_internal_engine_paths(
                workspace_dir,
                engine=engine,
                stage_dirname=stage_dirname,
            )
            runtime_root = runtime_paths["allowed_root"]
            if not runtime_root.exists() and not runtime_paths["organized_root"].exists():
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
    deps: ActivityListDeps,
) -> tuple[str, ...]:
    return deps._unique_texts(
        [
            normalize_text(entry.queue_id),
            normalize_text(entry.task_id),
            *list(deps._path_aliases(path_text, root=allowed_root)),
        ]
    )


def _engine_queue_record(
    entry: QueueEntry,
    *,
    app_name: str,
    engine: str,
    allowed_root: Path,
    deps: ActivityListDeps,
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
    return ActivityRecord(
        activity_id=normalize_text(entry.queue_id) or normalize_text(entry.task_id),
        kind="job",
        engine=engine,
        status=queue_entry_status(entry),
        label=_queue_record_label(entry, metadata, path_text),
        source=app_name,
        submitted_at=enqueued_at,
        updated_at=updated_at,
        cancel_target=normalize_text(entry.queue_id),
        aliases=_queue_record_aliases(
            entry,
            path_text,
            allowed_root=allowed_root,
            deps=deps,
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
            **deps._timestamp_metadata(
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
    deps: ActivityListDeps,
) -> list[ActivityRecord]:
    rows: list[ActivityRecord] = []
    for allowed_root in engine_queue_roots(config_path, engine=engine, deps=deps):
        for entry in deps.list_queue(allowed_root):
            rows.append(
                _engine_queue_record(
                    entry,
                    app_name=app_name,
                    engine=engine,
                    allowed_root=allowed_root,
                    deps=deps,
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
    deps: ActivityListDeps,
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
        deps=deps,
    )


def collect_crest_activity(
    resolved: ResolvedActivitySources,
    request: ActivityListRequest,
    *,
    deps: ActivityListDeps,
) -> list[ActivityRecord]:
    return collect_child_queue_activity(
        resolved,
        request,
        app_name="orca_auto_crest",
        engine="crest",
        config_path=resolved.crest_config,
        deps=deps,
    )


def collect_xtb_activity(
    resolved: ResolvedActivitySources,
    request: ActivityListRequest,
    *,
    deps: ActivityListDeps,
) -> list[ActivityRecord]:
    return collect_child_queue_activity(
        resolved,
        request,
        app_name="orca_auto_xtb",
        engine="xtb",
        config_path=resolved.xtb_config,
        deps=deps,
    )


def collect_orca_activity(
    resolved: ResolvedActivitySources,
    request: ActivityListRequest,
    *,
    deps: ActivityListDeps,
) -> list[ActivityRecord]:
    del request
    if not normalize_text(resolved.orca_config):
        return []
    return deps._orca_records(
        config_path=str(resolved.orca_config),
    )


__all__ = [
    "_engine_queue_record",
    "_queue_record_aliases",
    "_queue_record_label",
    "collect_child_queue_activity",
    "collect_crest_activity",
    "collect_orca_activity",
    "collect_xtb_activity",
    "engine_queue_records",
    "engine_queue_roots",
    "queue_entry_status",
    "requested_child_engines",
    "runtime_paths_for_engine",
]
