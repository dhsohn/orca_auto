from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..statuses import (
    ACTIVE_STATUSES,
    STATUS_CANCEL_REQUESTED,
    TERMINAL_STATUSES,
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
    """A canonical in-memory row: disk readers validate; producers supply typed fields."""

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


def effective_queue_status(entry: QueueEntry) -> str:
    """Return the display status of a queue row.

    A running row with a pending cancel request shows as ``cancel_requested``;
    every other row shows its persisted ``QueueStatus`` value.
    """

    status = entry.status.value
    if status == QueueStatus.RUNNING.value and entry.cancel_requested:
        return STATUS_CANCEL_REQUESTED
    return status


def entry_status_is_running(entry: QueueEntry | None) -> bool:
    return entry is not None and entry.status == QueueStatus.RUNNING
