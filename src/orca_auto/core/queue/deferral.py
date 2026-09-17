"""Admission deferral: a pending row that asked not to be claimed again yet.

A worker child records it when a resource the engine needs was refused before
the engine started, together with the requeue that returns its row to pending.
It is lifecycle metadata, not submission identity. It is inert once its time
has passed, and the next claim of the row removes it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from orca_auto.core.utils.persistence import parse_iso_utc

ADMISSION_DEFERRAL_METADATA_KEY = "admission_deferral"
ADMISSION_DEFERRAL_INTERVAL_SECONDS = 60.0


def admission_deferral_update(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    current = now if now is not None else datetime.now(UTC)
    not_before = current + timedelta(seconds=ADMISSION_DEFERRAL_INTERVAL_SECONDS)
    return {
        ADMISSION_DEFERRAL_METADATA_KEY: {
            "reason": str(reason),
            "not_before": not_before.isoformat(),
        }
    }


def _deferral(entry: Any) -> dict[str, Any] | None:
    metadata = getattr(entry, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    deferral = metadata.get(ADMISSION_DEFERRAL_METADATA_KEY)
    return deferral if isinstance(deferral, dict) else None


def queue_entry_admission_is_deferred(entry: Any, *, now: datetime | None = None) -> bool:
    deferral = _deferral(entry)
    if deferral is None:
        return False
    not_before = parse_iso_utc(deferral.get("not_before"))
    if not_before is None:
        return False
    current = now if now is not None else datetime.now(UTC)
    remaining = (not_before - current).total_seconds()
    # The wall clock can step backwards (WSL2 skew corrections). A wait longer
    # than one interval cannot have been written against this clock, so it is
    # treated as due instead of parking the row.
    return 0 < remaining <= ADMISSION_DEFERRAL_INTERVAL_SECONDS


def queue_entry_admission_deferral_reason(entry: Any) -> str:
    deferral = _deferral(entry)
    if deferral is None:
        return ""
    reason = deferral.get("reason")
    return reason.strip() if isinstance(reason, str) else ""


__all__ = [
    "ADMISSION_DEFERRAL_INTERVAL_SECONDS",
    "ADMISSION_DEFERRAL_METADATA_KEY",
    "admission_deferral_update",
    "queue_entry_admission_deferral_reason",
    "queue_entry_admission_is_deferred",
]
