from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ._activity_model import ActivityListRequest, ActivityRecord, ActivitySourceRequest


@dataclass(frozen=True)
class ActivityListProvider:
    source: str
    collect: Callable[[Any, ActivityListRequest], list[ActivityRecord]]


@dataclass(frozen=True)
class ActivityListDeps:
    list_workflow_registry: Callable[..., Any]
    reindex_workflow_registry: Callable[..., Any]
    list_workflow_summaries: Callable[..., Any]
    select_current_stage: Callable[..., Any]
    list_queue: Callable[..., Any]
    shared_workflow_root_from_config: Callable[..., Any]
    iter_workflow_runtime_workspaces: Callable[..., Any]
    workflow_workspace_internal_engine_paths: Callable[..., Any]
    engine_runtime_paths: Callable[..., Any]
    _coerce_mapping: Callable[..., dict[str, Any]]
    _mapping_text: Callable[..., str]
    _path_aliases: Callable[..., tuple[str, ...]]
    _sort_key: Callable[[ActivityRecord], Any]
    _timestamp_metadata: Callable[..., dict[str, str]]
    _unique_texts: Callable[..., tuple[str, ...]]
    _orca_records: Callable[..., list[ActivityRecord]]
    _resolved_activity_sources_for_request: Callable[[ActivitySourceRequest], Any]


__all__ = ["ActivityListDeps", "ActivityListProvider"]
