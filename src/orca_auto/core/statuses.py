"""Cross-layer status vocabulary shared by every state store's listing/display path.

Queue rows (``queue.json``) hold ``QueueStatus`` members whose values are the
``pending``/``running``/``completed``/``failed``/``cancelled`` strings below;
generation state (``job_state.json``) holds ``RunStatus`` values (``created``,
``running``, ``retrying``, ``completed``, ``failed``, ``cancelled``); job
location records use ``queued`` plus the terminal trio.  Everything else here
is a derived display status (``cancel_requested`` overlay, ``unknown``,
failure variants) that no store persists.
"""

from __future__ import annotations

STATUS_CANCEL_REQUESTED = "cancel_requested"
STATUS_CANCELLED = "cancelled"
STATUS_COMPLETED = "completed"
STATUS_CREATED = "created"
STATUS_ERROR = "error"
STATUS_FAILED = "failed"
STATUS_PENDING = "pending"
STATUS_QUEUED = "queued"
STATUS_REPAIR_BLOCKED = "repair_blocked"
STATUS_RETRYING = "retrying"
STATUS_RUNNING = "running"
STATUS_UNKNOWN = "unknown"

# Queue-row partition: every ``QueueStatus`` value is in exactly one of these two
# sets (``core.queue.types`` derives its ``QueueStatus`` sets from them).
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED})
ACTIVE_STATUSES = frozenset({STATUS_PENDING, STATUS_RUNNING})

# Display groupings over the union of store vocabularies.
FAILED_STATUSES = frozenset({STATUS_FAILED, STATUS_ERROR, STATUS_REPAIR_BLOCKED})
QUEUE_ACTIVE_STATUSES = frozenset(
    {
        STATUS_CREATED,
        STATUS_PENDING,
        STATUS_QUEUED,
        STATUS_RUNNING,
        STATUS_RETRYING,
        STATUS_CANCEL_REQUESTED,
    }
)
CANCEL_ACK_STATUSES = frozenset({STATUS_CANCELLED, STATUS_CANCEL_REQUESTED})


def normalize_status(value: object) -> str:
    return str(value or "").strip().lower()


def status_in(value: object, statuses: frozenset[str] | set[str] | tuple[str, ...]) -> bool:
    return normalize_status(value) in statuses


def is_failed_status(value: object) -> bool:
    return status_in(value, FAILED_STATUSES)


def is_queue_active_status(value: object) -> bool:
    return status_in(value, QUEUE_ACTIVE_STATUSES)


def is_cancel_ack_status(value: object) -> bool:
    return status_in(value, CANCEL_ACK_STATUSES)


__all__ = [
    "ACTIVE_STATUSES",
    "CANCEL_ACK_STATUSES",
    "FAILED_STATUSES",
    "QUEUE_ACTIVE_STATUSES",
    "STATUS_CANCEL_REQUESTED",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_CREATED",
    "STATUS_ERROR",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_QUEUED",
    "STATUS_REPAIR_BLOCKED",
    "STATUS_RETRYING",
    "STATUS_RUNNING",
    "STATUS_UNKNOWN",
    "TERMINAL_STATUSES",
    "is_cancel_ack_status",
    "is_failed_status",
    "is_queue_active_status",
    "normalize_status",
    "status_in",
]
