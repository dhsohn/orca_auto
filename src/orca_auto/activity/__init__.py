from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from orca_auto.core.activity import (
    ActivityCancelRequest,
    ActivityListRequest,
    ActivityRecord,
    ActivitySourceRequest,
    ResolvedActivitySources,
)

from . import _cancel as _activity_cancel
from . import _clear as _activity_clear
from . import _collectors as _activity_collectors
from . import _list as _activity_list
from . import _sources as _activity_sources


def list_activities(
    *,
    shared_config: str | None = None,
    refresh: bool = False,
    limit: int = 0,
    orca_config: str | None = None,
    engines: Sequence[str] = (),
    statuses: Sequence[str] = (),
    kinds: Sequence[str] = (),
) -> dict[str, Any]:
    return _activity_list.list_activities(
        shared_config=shared_config,
        refresh=refresh,
        limit=limit,
        orca_config=orca_config,
        engines=engines,
        statuses=statuses,
        kinds=kinds,
    )


def clear_activities(
    *,
    shared_config: str | None = None,
    orca_config: str | None = None,
) -> dict[str, Any]:
    return _activity_clear.clear_activities(
        shared_config=shared_config,
        orca_config=orca_config,
    )


def cancel_activity(
    *,
    target: str,
    shared_config: str | None = None,
    orca_config: str | None = None,
) -> dict[str, Any]:
    request = ActivityCancelRequest(
        target=target,
        sources=ActivitySourceRequest(
            shared_config=shared_config,
            orca_config=orca_config,
        ),
    )
    resolved = _activity_sources.resolve_activity_source_request(request.sources)
    record = _activity_cancel.match_activity_record(
        _activity_collectors.collect_activity_records(
            refresh=False,
            orca_config=resolved.orca_config,
        ),
        request.target,
    )

    result = _activity_cancel.cancel_orca_activity(record, resolved, request)
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
