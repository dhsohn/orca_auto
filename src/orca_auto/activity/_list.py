from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from orca_auto.activity_view import (
    count_global_active_simulations,
    filter_activity_items,
    normalize_activity_filter_values,
)
from orca_auto.core.activity import ActivityListRequest, ActivitySourceRequest
from orca_auto.core.utils import normalize_text

from . import _sources
from ._collectors import collect_activity_records_from_request


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
    request = ActivityListRequest(
        sources=ActivitySourceRequest(
            shared_config=shared_config,
            orca_config=orca_config,
        ),
        refresh=refresh,
        limit=limit,
        indexed=True,
        engines=normalize_activity_filter_values(engines),
        statuses=normalize_activity_filter_values(statuses),
        kinds=normalize_activity_filter_values(kinds),
    )
    resolved = _sources.resolve_activity_source_request(request.sources)
    records = collect_activity_records_from_request(request)
    all_items = [record.to_dict() for record in records]
    items = filter_activity_items(
        all_items, engines=request.engines, statuses=request.statuses, kinds=request.kinds
    )
    if request.limit > 0:
        items = items[: request.limit]
    blockers = []
    for record in records:
        metadata = record.metadata
        if metadata.get("publication_blocked_reason"):
            blockers.append(
                {
                    "queue_id": metadata.get("queue_id", record.activity_id),
                    "allowed_root": metadata.get("allowed_root", ""),
                    "scope": metadata.get("publication_blocked_scope", ""),
                    "reason": metadata["publication_blocked_reason"],
                    "next_action": metadata.get("publication_blocked_action", ""),
                }
            )
    return {
        "count": len(items),
        "activities": items,
        "active_simulations": count_global_active_simulations(
            all_items, config_path=resolved.orca_config
        ),
        **({"admission_blockers": blockers} if blockers else {}),
        "sources": {"orca_config": normalize_text(resolved.orca_config)},
    }


__all__ = ["list_activities"]
