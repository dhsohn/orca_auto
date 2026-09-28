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

# ``queue list`` counts these rows as active simulations when the admission
# store cannot be read; the slot count is the rule otherwise.
ACTIVE_SIMULATION_STATUSES = frozenset({STATUS_RUNNING, STATUS_RETRYING, STATUS_CANCEL_REQUESTED})
# Rows whose worker log ``queue list`` names: a running job an operator may want
# to tail, or a failed one whose log explains the failure.
WORKER_LOG_STATUSES = frozenset({STATUS_RUNNING, *FAILED_STATUSES})

# ``queue list`` summary buckets in display order: key, label, and the status
# whose icon and colour draw the bucket. ``pending`` gathers every active status
# but running (created, pending, queued, retrying, cancel_requested) under the
# queued glyph, so a bucket glyph illustrates its group rather than every row.
SUMMARY_BUCKETS = (
    ("running", "running", STATUS_RUNNING),
    ("pending", "queued", STATUS_QUEUED),
    ("done", "done", STATUS_COMPLETED),
    ("failed", "failed", STATUS_FAILED),
    ("cancelled", "cancelled", STATUS_CANCELLED),
    ("other", "other", STATUS_UNKNOWN),
)


def normalize_status(value: object) -> str:
    return str(value or "").strip().lower()


def is_queue_active_status(value: object) -> bool:
    return normalize_status(value) in QUEUE_ACTIVE_STATUSES


def summary_bucket(value: object) -> str:
    """The ``SUMMARY_BUCKETS`` key one row counts under."""
    status = normalize_status(value)
    if status == STATUS_RUNNING:
        return "running"
    if status in QUEUE_ACTIVE_STATUSES:
        return "pending"
    if status == STATUS_COMPLETED:
        return "done"
    if status in FAILED_STATUSES:
        return "failed"
    if status == STATUS_CANCELLED:
        return "cancelled"
    return "other"


__all__ = [
    "ACTIVE_SIMULATION_STATUSES",
    "ACTIVE_STATUSES",
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
    "SUMMARY_BUCKETS",
    "TERMINAL_STATUSES",
    "WORKER_LOG_STATUSES",
    "is_queue_active_status",
    "normalize_status",
    "summary_bucket",
]
