"""Listing: filter and page the catalog exactly once, then add the global active count."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orca_auto.activity.model import (
    ActivityListing,
    ActivityListRequest,
    admission_blocker,
    listing_from_records,
)
from orca_auto.core.admission import (
    AdmissionStoreCorruptError,
    admission_dir,
    read_active_slot_count,
)
from orca_auto.core.statuses import normalize_status
from orca_auto.orca.job_locations import rebuild_job_location_records

from . import _orca, _orca_index

LOGGER = logging.getLogger(__name__)


def normalize_activity_filter_values(values: Sequence[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = normalize_status(value)
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return tuple(normalized)


def global_active_simulations(
    runs_root: Path, *, fallback: int
) -> tuple[int, dict[str, Any] | None]:
    """The admission store's live slot count, else the catalog's own count.

    The slot count is the global truth across every consumer of the runtime;
    ``fallback`` is the listing's count of active job rows, used only when the
    store cannot be read. A corrupt store is returned as an
    ``admission_blockers`` payload next to the fallback count: the worker
    admits nothing while that file does not load.
    """
    admission_root = admission_dir(runs_root)
    try:
        return max(0, int(read_active_slot_count(admission_root))), None
    except AdmissionStoreCorruptError as exc:
        return max(0, int(fallback)), admission_blocker(
            queue_id="*",
            allowed_root=str(runs_root),
            scope="admission_store",
            reason=str(exc),
            next_action=(
                "Repair or remove the admission slot file; the worker admits no job until it loads."
            ),
        )
    except OSError as exc:
        LOGGER.debug(
            "active_simulation_slot_count_failed: admission_root=%s error=%s",
            admission_root,
            exc,
        )
    return max(0, int(fallback)), None


def collect_activity_listing(runs_root: Path, request: ActivityListRequest) -> ActivityListing:
    """The one place a list is filtered and paged: SQL for the projection, the
    shared in-memory pass for the disk catalog."""
    if request.indexed:
        if request.refresh:
            # Disk discoveries are durable: they land in job_locations.json
            # through the store upsert, whose publication makes the projection
            # (this query and every plain one after it) materialize them.
            rebuild_job_location_records(runs_root, apply=True)
        return _orca_index.query_listing(runs_root, request)
    return listing_from_records(
        _orca.orca_records(runs_root),
        statuses=request.statuses,
        limit=request.limit,
    )


def list_activities(
    *,
    config_path: str,
    runs_root: Path,
    refresh: bool = False,
    limit: int = 0,
    statuses: Sequence[str] = (),
) -> dict[str, Any]:
    listing = collect_activity_listing(
        runs_root,
        ActivityListRequest(
            refresh=refresh,
            limit=limit,
            indexed=True,
            statuses=normalize_activity_filter_values(statuses),
        ),
    )
    items = [record.to_dict() for record in listing.records]
    active_simulations, store_blocker = global_active_simulations(
        runs_root, fallback=listing.active_count
    )
    blockers = [*listing.blockers, *([store_blocker] if store_blocker else [])]
    return {
        "count": len(items),
        "active_simulations": active_simulations,
        "activities": items,
        "sources": {"orca_config": config_path},
        **({"admission_blockers": blockers} if blockers else {}),
    }


__all__ = [
    "collect_activity_listing",
    "global_active_simulations",
    "list_activities",
    "normalize_activity_filter_values",
]
