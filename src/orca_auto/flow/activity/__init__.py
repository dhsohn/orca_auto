from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.config.files import (
    shared_workflow_root_from_config,
)
from orca_auto.core.engines import get_engine_definition
from orca_auto.core.queue import clear_terminal as clear_queue_terminal
from orca_auto.core.queue import list_queue, request_cancel

from ..engine_options import WorkflowEngineOptions
from ..engine_runtime import engine_runtime_paths
from ..registry import (
    clear_terminal_workflow_registry,
    list_workflow_registry,
    reindex_workflow_registry,
)
from ..state import (
    iter_workflow_runtime_workspaces,
    list_workflow_summaries,
    workflow_workspace_internal_engine_paths,
)
from ..submitters.crest import cancel_target as cancel_crest_target
from ..submitters.orca import cancel_target as cancel_orca_target
from ..submitters.xtb import cancel_target as cancel_xtb_target
from ..workflow.status import WORKFLOW_TERMINAL_STATUSES, select_current_stage
from . import _cancel as _activity_cancel
from . import _clear as _activity_clear
from . import _list as _activity_list
from . import _orca as _activity_orca
from . import _sources as _activity_sources
from ._model import (
    ActivityCancelRequest,
    ActivityListRequest,
    ActivityRecord,
    ActivitySourceRequest,
    ResolvedActivitySources,
)
from ._model import (
    mapping_text as _mapping_text,
)
from ._model import (
    path_aliases as _path_aliases,
)
from ._model import (
    sort_key as _sort_key,
)
from ._model import (
    timestamp_metadata as _timestamp_metadata,
)
from ._model import (
    unique_texts as _unique_texts,
)

_ACTIVITY_CLEARABLE_TERMINAL_STATUSES = WORKFLOW_TERMINAL_STATUSES


@dataclass(frozen=True)
class _OrcaActivityDeps:
    engine_runtime_paths: Any
    _unique_texts: Any
    _path_aliases: Any
    _timestamp_metadata: Any


@dataclass(frozen=True)
class _ActivityCancelDeps:
    cancel_engine_targets: dict[str, Any]
    cancel_orca_target: Any
    _discover_orca_repo_root: Any
    engine_queue_roots: Any
    list_queue: Any
    request_cancel: Any
    get_engine_definition: Any


def _orca_activity_deps() -> _OrcaActivityDeps:
    return _OrcaActivityDeps(
        engine_runtime_paths=engine_runtime_paths,
        _unique_texts=_unique_texts,
        _path_aliases=_path_aliases,
        _timestamp_metadata=_timestamp_metadata,
    )


def _activity_cancel_deps() -> _ActivityCancelDeps:
    def engine_queue_roots(config_path: str, *, engine: str) -> tuple[Path, ...]:
        return _activity_list.engine_queue_roots(
            config_path,
            engine=engine,
            deps=_activity_list_deps(),
        )

    return _ActivityCancelDeps(
        cancel_engine_targets={
            "crest": cancel_crest_target,
            "xtb": cancel_xtb_target,
        },
        cancel_orca_target=cancel_orca_target,
        _discover_orca_repo_root=_activity_sources.discover_orca_repo_root,
        engine_queue_roots=engine_queue_roots,
        list_queue=list_queue,
        request_cancel=request_cancel,
        get_engine_definition=get_engine_definition,
    )


def _activity_list_deps() -> _activity_list.ActivityListDeps:
    def orca_records(*, config_path: str) -> list[ActivityRecord]:
        return _activity_orca.orca_records(
            config_path=config_path,
            deps=_orca_activity_deps(),
        )

    return _activity_list.ActivityListDeps(
        list_workflow_registry=list_workflow_registry,
        reindex_workflow_registry=reindex_workflow_registry,
        list_workflow_summaries=list_workflow_summaries,
        select_current_stage=select_current_stage,
        list_queue=list_queue,
        shared_workflow_root_from_config=shared_workflow_root_from_config,
        iter_workflow_runtime_workspaces=iter_workflow_runtime_workspaces,
        workflow_workspace_internal_engine_paths=workflow_workspace_internal_engine_paths,
        engine_runtime_paths=engine_runtime_paths,
        _coerce_mapping=_activity_sources.coerce_mapping,
        _mapping_text=_mapping_text,
        _path_aliases=_path_aliases,
        _sort_key=_sort_key,
        _timestamp_metadata=_timestamp_metadata,
        _unique_texts=_unique_texts,
        _orca_records=orca_records,
        _resolved_activity_sources_for_request=_activity_sources.resolve_activity_source_request,
    )


def _activity_clear_deps() -> _activity_clear.ActivityClearDeps:
    def engine_queue_roots(config_path: str, *, engine: str) -> tuple[Path, ...]:
        return _activity_list.engine_queue_roots(
            config_path,
            engine=engine,
            deps=_activity_list_deps(),
        )

    return _activity_clear.ActivityClearDeps(
        _resolved_activity_sources_for_request=_activity_sources.resolve_activity_source_request,
        clear_terminal_workflow_registry=clear_terminal_workflow_registry,
        clear_queue_terminal=clear_queue_terminal,
        _engine_queue_roots=engine_queue_roots,
        engine_runtime_paths=engine_runtime_paths,
    )


def list_activities(
    *,
    workflow_root: str | Path | None = None,
    shared_config: str | None = None,
    refresh: bool = False,
    limit: int = 0,
    crest_config: str | None = None,
    xtb_config: str | None = None,
    orca_config: str | None = None,
    child_job_engines: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    return _activity_list.list_activities(
        workflow_root=workflow_root,
        shared_config=shared_config,
        refresh=refresh,
        limit=limit,
        crest_config=crest_config,
        xtb_config=xtb_config,
        orca_config=orca_config,
        child_job_engines=child_job_engines,
        deps=_activity_list_deps(),
    )


def clear_activities(
    *,
    workflow_root: str | Path | None = None,
    shared_config: str | None = None,
    crest_config: str | None = None,
    xtb_config: str | None = None,
    orca_config: str | None = None,
) -> dict[str, Any]:
    return _activity_clear.clear_activities(
        workflow_root=workflow_root,
        shared_config=shared_config,
        crest_config=crest_config,
        xtb_config=xtb_config,
        orca_config=orca_config,
        clearable_terminal_statuses=_ACTIVITY_CLEARABLE_TERMINAL_STATUSES,
        deps=_activity_clear_deps(),
    )


def cancel_activity(
    *,
    target: str,
    workflow_root: str | Path | None = None,
    shared_config: str | None = None,
    crest_config: str | None = None,
    xtb_config: str | None = None,
    orca_config: str | None = None,
    orca_repo_root: str | None = None,
) -> dict[str, Any]:
    request = ActivityCancelRequest(
        target=target,
        sources=ActivitySourceRequest(
            workflow_root=workflow_root,
            shared_config=shared_config,
            crest_config=crest_config,
            xtb_config=xtb_config,
            orca_config=orca_config,
        ),
        engine_options=WorkflowEngineOptions.from_values(
            shared_config=shared_config,
            crest_config=crest_config,
            xtb_config=xtb_config,
            orca_config=orca_config,
            orca_repo_root=orca_repo_root,
        ),
    )
    resolved = _activity_sources.resolve_activity_source_request(request.sources)
    record = _activity_cancel.match_activity_record(
        _activity_list.collect_activity_records(
            workflow_root=resolved.workflow_root,
            refresh=False,
            crest_config=resolved.crest_config,
            xtb_config=resolved.xtb_config,
            orca_config=resolved.orca_config,
            deps=_activity_list_deps(),
        ),
        request.target,
    )

    if record.kind == "workflow":
        result = _activity_cancel.cancel_workflow_activity(record, resolved, request)
        return _activity_cancel.cancel_activity_payload(
            record,
            result,
            fallback_status="cancelled",
        )

    result = _activity_cancel.cancel_non_workflow_activity(
        record,
        resolved,
        request,
        deps=_activity_cancel_deps(),
    )
    return _activity_cancel.cancel_activity_payload(record, result, fallback_status="failed")


__all__ = [
    "ActivityCancelRequest",
    "ActivityListRequest",
    "ActivityRecord",
    "ActivitySourceRequest",
    "ResolvedActivitySources",
    "cancel_activity",
    "clear_activities",
    "list_activities",
]
