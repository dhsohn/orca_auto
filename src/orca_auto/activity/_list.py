"""Listing: resolve the config, then filter and page the catalog exactly once."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from orca_auto.activity.model import (
    ActivityListing,
    ActivityListRequest,
    ActivityRecord,
    ActivitySourceRequest,
    ResolvedActivitySources,
    listing_from_records,
)
from orca_auto.activity_view import global_active_simulations, normalize_activity_filter_values
from orca_auto.core.config import discovery
from orca_auto.core.utils import normalize_text
from orca_auto.orca.engine_runtime import engine_runtime_paths
from orca_auto.orca.job_locations import rebuild_job_location_records

from . import _orca, _orca_index


def resolve_activity_sources(request: ActivitySourceRequest) -> ResolvedActivitySources:
    config = normalize_text(request.orca_config) or normalize_text(request.shared_config)
    config_path = discovery.resolve_shared_config_path(config or None)
    if not config_path:
        # Without a config there is no queue to list, clear or cancel in; an
        # empty listing here would read as "nothing queued".
        raise ValueError(
            "No orca_auto.yaml found: pass --config, set ORCA_AUTO_CONFIG, "
            "or create ~/orca_auto/config/orca_auto.yaml."
        )
    return ResolvedActivitySources(orca_config=config_path)


def collect_activity_listing(
    resolved: ResolvedActivitySources,
    request: ActivityListRequest,
) -> ActivityListing:
    """The one place a list is filtered and paged: SQL for the projection, the
    shared in-memory pass for the disk catalog."""
    config_path = normalize_text(resolved.orca_config)
    if request.indexed:
        root = engine_runtime_paths(config_path)["allowed_root"]
        if request.refresh:
            # Disk discoveries are durable: they land in job_locations.json
            # through the store upsert, whose publication makes the projection
            # (this query and every plain one after it) materialize them.
            rebuild_job_location_records(root, apply=True)
        return _orca_index.query_listing(root, request)
    return listing_from_records(
        _orca.orca_records(config_path=config_path),
        statuses=request.statuses,
        limit=request.limit,
    )


def collect_activity_records(
    resolved: ResolvedActivitySources,
    request: ActivityListRequest,
) -> list[ActivityRecord]:
    return list(collect_activity_listing(resolved, request).records)


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
    listing = collect_activity_listing(resolved, request)
    items = [record.to_dict() for record in listing.records]
    active_simulations, store_blocker = global_active_simulations(
        config_path=resolved.orca_config, fallback=listing.active_count
    )
    blockers = [*listing.blockers, *([store_blocker] if store_blocker else [])]
    return {
        "count": len(items),
        "active_simulations": active_simulations,
        "activities": items,
        "sources": {"orca_config": normalize_text(resolved.orca_config)},
        **({"admission_blockers": blockers} if blockers else {}),
    }


__all__ = [
    "collect_activity_listing",
    "collect_activity_records",
    "list_activities",
    "resolve_activity_sources",
]
