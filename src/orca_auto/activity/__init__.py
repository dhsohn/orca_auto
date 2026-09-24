"""Public activity catalog API: list, clear and cancel across the ORCA queue."""

from __future__ import annotations

from typing import Any

from orca_auto.activity.model import (
    ActivityCancelRequest,
    ActivityListRequest,
    ActivityRecord,
    ActivitySourceRequest,
    ResolvedActivitySources,
)
from orca_auto.core.statuses import STATUS_FAILED

from . import _cancel as _activity_cancel
from . import _list as _activity_list
from ._clear import clear_activities
from ._list import list_activities


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
    resolved = _activity_list.resolve_activity_sources(request.sources)
    record = _activity_cancel.match_activity_record(
        _activity_list.collect_activity_records(
            resolved, ActivityListRequest(sources=request.sources, refresh=False)
        ),
        request.target,
    )

    result = _activity_cancel.cancel_orca_activity(record, resolved, request)
    return _activity_cancel.cancel_activity_payload(record, result, fallback_status=STATUS_FAILED)


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
