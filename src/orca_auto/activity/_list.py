"""Listing: filter and page the catalog exactly once, then add the global active count."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orca_auto.activity.model import admission_blocker, listing_from_records
from orca_auto.core.admission import (
    AdmissionStoreCorruptError,
    admission_dir,
    read_active_slot_count,
)

from . import _orca

LOGGER = logging.getLogger(__name__)


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


def list_activities(
    *,
    config_path: str,
    runs_root: Path,
    limit: int = 0,
    statuses: Sequence[str] = (),
) -> dict[str, Any]:
    listing = listing_from_records(
        (record for _entry, record in _orca.catalog(runs_root)), statuses=statuses, limit=limit
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


__all__ = ["global_active_simulations", "list_activities"]
