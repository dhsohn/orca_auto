from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.activity import (
    ActivityListRequest,
    ActivityRecord,
    ActivitySourceRequest,
    ResolvedActivitySources,
    sort_key,
)
from orca_auto.core.engine_catalog import EngineCatalogEntry, activity_engine_entries
from orca_auto.core.extensions import workflows_available
from orca_auto.core.utils import normalize_text

from . import _sources
from ._queue_records import collect_catalog_engine_activity


@dataclass(frozen=True)
class ActivityListProvider:
    source: str
    collect: Callable[[Any, ActivityListRequest], list[ActivityRecord]]


def collect_workflow_activity(
    resolved: ResolvedActivitySources,
    request: ActivityListRequest,
) -> list[ActivityRecord]:
    if not normalize_text(resolved.workflow_root):
        return []
    from orca_auto.flow.activity._workflow_records import workflow_records

    return workflow_records(
        workflow_root=str(resolved.workflow_root),
        refresh=request.refresh,
    )


def _catalog_activity_provider(entry: EngineCatalogEntry) -> ActivityListProvider:
    def collect(
        resolved: ResolvedActivitySources,
        request: ActivityListRequest,
    ) -> list[ActivityRecord]:
        return collect_catalog_engine_activity(entry, resolved, request)

    return ActivityListProvider(entry.source_id, collect)


def activity_list_providers() -> tuple[ActivityListProvider, ...]:
    providers = (
        [ActivityListProvider("orca_auto_flow", collect_workflow_activity)]
        if workflows_available()
        else []
    )
    for entry in activity_engine_entries():
        providers.append(_catalog_activity_provider(entry))
    return tuple(providers)


def collect_activity_records_from_request(
    request: ActivityListRequest,
) -> list[ActivityRecord]:
    resolved = _sources.resolve_activity_source_request(request.sources)
    _sources.require_activity_support(resolved)
    rows: list[ActivityRecord] = []
    for provider in activity_list_providers():
        rows.extend(provider.collect(resolved, request))
    return sorted(rows, key=sort_key, reverse=True)


def collect_activity_records(
    *,
    workflow_root: str | Path | None = None,
    shared_config: str | None = None,
    refresh: bool = False,
    crest_config: str | None = None,
    xtb_config: str | None = None,
    orca_config: str | None = None,
) -> list[ActivityRecord]:
    return collect_activity_records_from_request(
        ActivityListRequest(
            sources=ActivitySourceRequest(
                workflow_root=workflow_root,
                shared_config=shared_config,
                crest_config=crest_config,
                xtb_config=xtb_config,
                orca_config=orca_config,
            ),
            refresh=refresh,
        ),
    )


__all__ = [
    "ActivityListProvider",
    "activity_list_providers",
    "collect_activity_records",
    "collect_activity_records_from_request",
    "collect_workflow_activity",
]
