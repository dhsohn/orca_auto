from __future__ import annotations

STATUS_CANCEL_REQUESTED = "cancel_requested"
STATUS_CANCEL_FAILED = "cancel_failed"
STATUS_CANCELLED = "cancelled"
STATUS_ADMISSION_LIMIT_REACHED = "admission_limit_reached"
STATUS_BLOCKED = "blocked"
STATUS_COMPLETED = "completed"
STATUS_CREATED = "created"
STATUS_FAILED = "failed"
STATUS_PENDING = "pending"
STATUS_PLANNED = "planned"
STATUS_QUEUED = "queued"
STATUS_REPAIR_BLOCKED = "repair_blocked"
STATUS_RETRYING = "retrying"
STATUS_RUNNING = "running"
STATUS_SKIPPED = "skipped"
STATUS_SUBMISSION_FAILED = "submission_failed"
STATUS_SUBMITTED = "submitted"
STATUS_UNKNOWN = "unknown"
STATUS_WAITING_FOR_SLOT = "waiting_for_slot"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED})
FAILED_STATUSES = frozenset({STATUS_FAILED, STATUS_CANCEL_FAILED, STATUS_SUBMISSION_FAILED})
QUEUE_ACTIVE_STATUSES = frozenset(
    {
        STATUS_PLANNED,
        STATUS_PENDING,
        STATUS_QUEUED,
        STATUS_SUBMITTED,
        STATUS_RUNNING,
        STATUS_RETRYING,
        STATUS_CANCEL_REQUESTED,
    }
)
CANCEL_ACK_STATUSES = frozenset({STATUS_CANCELLED, STATUS_CANCEL_REQUESTED})
SUBMISSION_DEFERRED_STATUSES = frozenset(
    {
        STATUS_BLOCKED,
        STATUS_WAITING_FOR_SLOT,
        STATUS_ADMISSION_LIMIT_REACHED,
    }
)


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
    "CANCEL_ACK_STATUSES",
    "FAILED_STATUSES",
    "QUEUE_ACTIVE_STATUSES",
    "STATUS_ADMISSION_LIMIT_REACHED",
    "STATUS_BLOCKED",
    "STATUS_CANCEL_FAILED",
    "STATUS_CANCEL_REQUESTED",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_CREATED",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_PLANNED",
    "STATUS_QUEUED",
    "STATUS_REPAIR_BLOCKED",
    "STATUS_RETRYING",
    "STATUS_RUNNING",
    "STATUS_SKIPPED",
    "STATUS_SUBMISSION_FAILED",
    "STATUS_SUBMITTED",
    "STATUS_UNKNOWN",
    "STATUS_WAITING_FOR_SLOT",
    "SUBMISSION_DEFERRED_STATUSES",
    "TERMINAL_STATUSES",
    "is_cancel_ack_status",
    "is_failed_status",
    "is_queue_active_status",
    "normalize_status",
    "status_in",
]
