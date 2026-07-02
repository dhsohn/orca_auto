from __future__ import annotations

from pathlib import Path

from orca_auto.core.app_ids import ORCA_AUTO_ORCA_SOURCE
from orca_auto.core.utils import normalize_text

from ._list_deps import ActivityListDeps, ActivityListProvider
from ._model import (
    ActivityListRequest,
    ActivityRecord,
    ActivitySourceRequest,
    ResolvedActivitySources,
)
from ._queue_records import (
    collect_crest_activity,
    collect_orca_activity,
    collect_xtb_activity,
)
from ._workflow_records import workflow_records


def collect_workflow_activity(
    resolved: ResolvedActivitySources,
    request: ActivityListRequest,
    *,
    deps: ActivityListDeps,
) -> list[ActivityRecord]:
    if not normalize_text(resolved.workflow_root):
        return []
    return workflow_records(
        workflow_root=str(resolved.workflow_root),
        refresh=request.refresh,
        deps=deps,
    )


def activity_list_providers(deps: ActivityListDeps) -> tuple[ActivityListProvider, ...]:
    return (
        ActivityListProvider(
            "orca_auto_flow",
            lambda resolved, request: collect_workflow_activity(
                resolved,
                request,
                deps=deps,
            ),
        ),
        ActivityListProvider(
            "orca_auto_crest",
            lambda resolved, request: collect_crest_activity(
                resolved,
                request,
                deps=deps,
            ),
        ),
        ActivityListProvider(
            "orca_auto_xtb",
            lambda resolved, request: collect_xtb_activity(
                resolved,
                request,
                deps=deps,
            ),
        ),
        ActivityListProvider(
            ORCA_AUTO_ORCA_SOURCE,
            lambda resolved, request: collect_orca_activity(
                resolved,
                request,
                deps=deps,
            ),
        ),
    )


def collect_activity_records_from_request(
    request: ActivityListRequest,
    *,
    deps: ActivityListDeps,
) -> list[ActivityRecord]:
    resolved = deps._resolved_activity_sources_for_request(request.sources)
    rows: list[ActivityRecord] = []
    for provider in activity_list_providers(deps):
        rows.extend(provider.collect(resolved, request))
    return sorted(rows, key=deps._sort_key, reverse=True)


def collect_activity_records(
    *,
    workflow_root: str | Path | None = None,
    shared_config: str | None = None,
    refresh: bool = False,
    crest_config: str | None = None,
    xtb_config: str | None = None,
    orca_config: str | None = None,
    child_job_engines: tuple[str, ...] | None = None,
    deps: ActivityListDeps,
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
            child_job_engines=child_job_engines,
        ),
        deps=deps,
    )


__all__ = [
    "activity_list_providers",
    "collect_activity_records",
    "collect_activity_records_from_request",
    "collect_workflow_activity",
]
