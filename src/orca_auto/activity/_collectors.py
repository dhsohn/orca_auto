from __future__ import annotations

from orca_auto.core.activity import (
    ActivityListRequest,
    ActivityRecord,
    ActivitySourceRequest,
    sort_key,
)

from . import _sources
from ._queue_records import collect_orca_activity


def collect_activity_records_from_request(request: ActivityListRequest) -> list[ActivityRecord]:
    resolved = _sources.resolve_activity_source_request(request.sources)
    return sorted(collect_orca_activity(resolved, request), key=sort_key, reverse=True)


def collect_activity_records(
    *,
    shared_config: str | None = None,
    refresh: bool = False,
    orca_config: str | None = None,
) -> list[ActivityRecord]:
    return collect_activity_records_from_request(
        ActivityListRequest(
            sources=ActivitySourceRequest(shared_config=shared_config, orca_config=orca_config),
            refresh=refresh,
        )
    )
