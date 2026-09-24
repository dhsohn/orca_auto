from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from orca_auto.activity_view import (
    count_global_active_simulations,
    filter_activity_items,
    normalize_activity_filter_values,
)
from orca_auto.core.activity import (
    ActivityListRequest,
    ActivityRecord,
    ActivitySourceRequest,
    ResolvedActivitySources,
    sort_key,
)
from orca_auto.core.config import discovery
from orca_auto.core.engine_runtime import engine_runtime_paths
from orca_auto.core.utils import normalize_text

from . import _orca, _orca_index


def resolve_activity_sources(request: ActivitySourceRequest) -> ResolvedActivitySources:
    config = normalize_text(request.orca_config) or normalize_text(request.shared_config)
    return ResolvedActivitySources(orca_config=discovery.resolve_shared_config_path(config or None))


def collect_activity_records(
    resolved: ResolvedActivitySources,
    request: ActivityListRequest,
) -> list[ActivityRecord]:
    config_path = normalize_text(resolved.orca_config)
    if not config_path:
        return []
    if request.indexed:
        root = engine_runtime_paths(config_path)["allowed_root"]
        indexed = _orca_index.query_records(root, request)
        if not request.refresh:
            return sorted(indexed, key=sort_key, reverse=True)
        # Explicit unindexed discoveries remain local to this one query.
    records = _orca.orca_records(config_path=config_path, refresh=request.refresh)
    return sorted(records, key=sort_key, reverse=True)


def list_activities(
    *,
    shared_config: str | None = None,
    refresh: bool = False,
    limit: int = 0,
    orca_config: str | None = None,
    statuses: Sequence[str] = (),
) -> dict[str, Any]:
    request = ActivityListRequest(
        sources=ActivitySourceRequest(
            shared_config=shared_config,
            orca_config=orca_config,
        ),
        refresh=refresh,
        limit=limit,
        indexed=True,
        statuses=normalize_activity_filter_values(statuses),
    )
    resolved = resolve_activity_sources(request.sources)
    records = collect_activity_records(resolved, request)
    all_items = [record.to_dict() for record in records]
    items = filter_activity_items(all_items, statuses=request.statuses)
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


__all__ = ["collect_activity_records", "list_activities", "resolve_activity_sources"]
