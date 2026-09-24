from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..statuses import (
    ACTIVE_STATUSES,
    STATUS_CANCEL_REQUESTED,
    STATUS_UNKNOWN,
    TERMINAL_STATUSES,
    normalize_status,
)


class QueueStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Derived from the single string definition in ``core.statuses``; constructing
# each member here guarantees every listed value is a real ``QueueStatus``.
TERMINAL_QUEUE_STATUSES: frozenset[QueueStatus] = frozenset(
    QueueStatus(value) for value in TERMINAL_STATUSES
)
ACTIVE_QUEUE_STATUSES: frozenset[QueueStatus] = frozenset(
    QueueStatus(value) for value in ACTIVE_STATUSES
)


@dataclass(frozen=True)
class QueueEntry:
    queue_id: str
    app_name: str
    task_id: str
    task_kind: str
    engine: str
    status: QueueStatus = QueueStatus.PENDING
    priority: int = 10
    enqueued_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    cancel_requested: bool = False
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def queue_status_value(entry: QueueEntry) -> str:
    """Return the persisted status string of ``entry`` (``unknown`` when absent)."""

    status = getattr(entry, "status", None)
    if isinstance(status, QueueStatus):
        return status.value
    return normalize_status(status) or STATUS_UNKNOWN


def effective_queue_status(entry: QueueEntry) -> str:
    """Return the display status of a queue row.

    A running row with a pending cancel request shows as ``cancel_requested``;
    every other row shows its persisted ``QueueStatus`` value.
    """

    status = queue_status_value(entry)
    if status == QueueStatus.RUNNING.value and bool(getattr(entry, "cancel_requested", False)):
        return STATUS_CANCEL_REQUESTED
    return status
