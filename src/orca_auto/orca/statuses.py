from __future__ import annotations

from collections.abc import Iterable
from enum import Enum


class RunStatus(str, Enum):
    """Generation lifecycle status persisted in ``job_state.json``."""

    CREATED = "created"
    RUNNING = "running"
    RETRYING = "retrying"  # Retired: immutable historical state reader only.
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)
# Statuses that imply a live attempt; a run parked here without a live run lock
# was interrupted and is either resumable or stale.
ACTIVE_RUN_STATUSES: frozenset[RunStatus] = frozenset({RunStatus.RUNNING, RunStatus.RETRYING})


def run_status_values(statuses: Iterable[RunStatus]) -> frozenset[str]:
    return frozenset(status.value for status in statuses)


TERMINAL_RUN_STATUS_VALUES = run_status_values(TERMINAL_RUN_STATUSES)
ACTIVE_RUN_STATUS_VALUES = run_status_values(ACTIVE_RUN_STATUSES)


def coerce_run_status(status: RunStatus | str) -> RunStatus:
    """Return the ``RunStatus`` member for ``status`` or raise ``ValueError``."""

    if isinstance(status, RunStatus):
        return status
    try:
        return RunStatus(str(status).strip().lower())
    except ValueError:
        raise ValueError(f"unknown run status: {status!r}") from None


class AnalyzerStatus(str, Enum):
    COMPLETED = "completed"
    ERROR_SCF = "error_scf"
    ERROR_SCFGRAD_ABORT = "error_scfgrad_abort"
    ERROR_MULTIPLICITY_IMPOSSIBLE = "error_multiplicity_impossible"
    ERROR_DISK_IO = "error_disk_io"
    ERROR_MEMORY = "error_memory"
    ERROR_GEOMETRY = "error_geometry"
    GEOM_NOT_CONVERGED = "geom_not_converged"
    TS_NOT_FOUND = "ts_not_found"
    INCOMPLETE = "incomplete"
    UNKNOWN_FAILURE = "unknown_failure"
