"""Listing: resolve the config, then filter and page the catalog exactly once."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from orca_auto.activity.model import (
    ActivityListing,
    ActivityListRequest,
    listing_from_records,
)
from orca_auto.activity_view import global_active_simulations, normalize_activity_filter_values
from orca_auto.core.config import discovery
from orca_auto.core.utils import normalize_text
from orca_auto.orca.engine_runtime import engine_runtime_paths
from orca_auto.orca.job_locations import rebuild_job_location_records

from . import _orca, _orca_index


def resolve_activity_config(config_path: str | None) -> str:
    """The config every list, clear and cancel reads: explicit, then discovered."""
    resolved = discovery.resolve_shared_config_path(normalize_text(config_path) or None)
    if not resolved:
        # Without a config there is no queue to list, clear or cancel in; an
        # empty listing here would read as "nothing queued".
        raise ValueError(
            "No orca_auto.yaml found: pass --config, set ORCA_AUTO_CONFIG, "
            "or create ~/orca_auto/config/orca_auto.yaml."
        )
    return resolved


def collect_activity_listing(config_path: str, request: ActivityListRequest) -> ActivityListing:
    """The one place a list is filtered and paged: SQL for the projection, the
    shared in-memory pass for the disk catalog."""
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


def list_activities(
    *,
    config_path: str | None = None,
    refresh: bool = False,
    limit: int = 0,
    statuses: Sequence[str] = (),
) -> dict[str, Any]:
    resolved = resolve_activity_config(config_path)
    listing = collect_activity_listing(
        resolved,
        ActivityListRequest(
            refresh=refresh,
            limit=limit,
            indexed=True,
            statuses=normalize_activity_filter_values(statuses),
        ),
    )
    items = [record.to_dict() for record in listing.records]
    active_simulations, store_blocker = global_active_simulations(
        config_path=resolved, fallback=listing.active_count
    )
    blockers = [*listing.blockers, *([store_blocker] if store_blocker else [])]
    return {
        "count": len(items),
        "active_simulations": active_simulations,
        "activities": items,
        "sources": {"orca_config": resolved},
        **({"admission_blockers": blockers} if blockers else {}),
    }


__all__ = [
    "collect_activity_listing",
    "list_activities",
    "resolve_activity_config",
]
