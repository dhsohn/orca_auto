"""The queued-record publication lease a queue row carries in its metadata.

One committed queue row must always end up with its queued job artifact
published exactly once, no matter where the publisher crashes. The lease is
the row's ``QUEUE_RECORD_SYNC_*`` metadata: the submitting driver
(``orca.queue.enqueue_publication``) takes and completes it, the worker's
repair pass (``orca.queue.publication_repair``) re-claims a parked one, and
cancellation (``transitions.request_cancel``) revokes it.

Protocol invariants:

- A row is enqueued with a PREPARING sync lease owned by the publishing
  process; the row is unclaimable until the lease is COMPLETE.
- Publication happens under the per-row publication lock: re-validate
  ownership, publish, then CAS the lease to COMPLETE. The COMPLETE
  short-circuit is token-verified — a COMPLETE written by another lease is
  ownership loss, never this publisher's success.
- Every failure after the durable commit parks the row as REPAIR_PENDING
  (owner 0) instead of publishing blind or failing terminally; the worker's
  pre-claim repair pass republishes it before the row can run.
- An enqueue whose commit outcome cannot be determined is reported as
  ``EnqueuePublicationOutcomeUnknown``, never as an ordinary failure.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..utils import process as process_utils
from ..utils.coercion import normalize_text
from ..utils.lock import file_lock
from ..utils.persistence import resolve_root_path
from .types import QueueEntry, QueueStatus

QUEUE_RECORD_SYNC_KEY = "_orca_auto_queued_record_sync"
QUEUE_RECORD_SYNC_UPDATED_AT_KEY = "_orca_auto_queued_record_sync_updated_at"
QUEUE_RECORD_SYNC_OWNER_PID_KEY = "_orca_auto_queued_record_sync_owner_pid"
QUEUE_RECORD_SYNC_OWNER_START_KEY = "_orca_auto_queued_record_sync_owner_start"
QUEUE_RECORD_SYNC_TOKEN_KEY = "_orca_auto_queued_record_sync_token"
QUEUE_RECORD_SYNC_BLOCKED_KEY = "_orca_auto_queued_record_sync_blocked"

QUEUE_RECORD_SYNC_PREPARING = "preparing"
QUEUE_RECORD_SYNC_REPAIR_PENDING = "repair_pending"
QUEUE_RECORD_SYNC_REPAIRING = "repairing"
QUEUE_RECORD_SYNC_COMPLETE = "complete"
QUEUE_RECORD_SYNC_ABORTED = "aborted"

# A lease still in flight: a repair may claim it and cancellation revokes it.
REPAIRABLE_SYNC_STATES = frozenset(
    {
        QUEUE_RECORD_SYNC_PREPARING,
        QUEUE_RECORD_SYNC_REPAIR_PENDING,
        QUEUE_RECORD_SYNC_REPAIRING,
    }
)

QUEUE_RECORD_PUBLICATION_LOCK_TIMEOUT_SECONDS = 300.0
_QUEUE_RECORD_PUBLICATION_LOCK_DIR = ".queue-publication-locks"


def queue_record_sync_state(entry: Any) -> str:
    metadata = getattr(entry, "metadata", {})
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get(QUEUE_RECORD_SYNC_KEY, "")).strip().lower()


def queue_record_sync_token(entry: QueueEntry) -> str:
    return normalize_text(entry.metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY))


def process_start_token(process_id: int) -> str:
    """Return a boot-scoped process-start identity when the OS exposes one.

    The token keeps field 22 as written (not the parsed ticks), so a stored
    token stays comparable byte for byte.
    """
    if process_id <= 0:
        return ""
    try:
        stat_text = Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8")
    except OSError:
        return ""
    start_ticks = process_utils.stat_starttime_field(stat_text)
    if start_ticks is None:
        return ""
    boot_id = process_utils.linux_boot_id()
    return f"{boot_id}:{start_ticks}" if boot_id else ""


def queue_record_sync_metadata(
    sync_state: str,
    *,
    token: str,
    owner_pid: int,
) -> dict[str, Any]:
    """Build the canonical fencing metadata for one publication lease."""
    normalized_owner_pid = int(owner_pid)
    metadata = {
        QUEUE_RECORD_SYNC_KEY: str(sync_state).strip().lower(),
        QUEUE_RECORD_SYNC_UPDATED_AT_KEY: datetime.now(UTC).isoformat(),
        QUEUE_RECORD_SYNC_OWNER_PID_KEY: normalized_owner_pid,
        QUEUE_RECORD_SYNC_OWNER_START_KEY: (
            process_start_token(normalized_owner_pid) if normalized_owner_pid > 0 else ""
        ),
        QUEUE_RECORD_SYNC_TOKEN_KEY: str(token).strip(),
    }
    if str(sync_state).strip().lower() in {QUEUE_RECORD_SYNC_COMPLETE, QUEUE_RECORD_SYNC_ABORTED}:
        metadata[QUEUE_RECORD_SYNC_BLOCKED_KEY] = None
    return metadata


def park_queue_record_repair_pending(
    entries: list[QueueEntry],
    entry: QueueEntry,
    *,
    expected_state: str,
    expected_token: str,
) -> tuple[None, bool]:
    """Mutator: token-gated CAS from one owned lease to REPAIR_PENDING (owner 0)."""

    for index, current in enumerate(entries):
        if current.queue_id != entry.queue_id:
            continue
        if (
            current.status != QueueStatus.PENDING
            or queue_record_sync_state(current) != expected_state
            or queue_record_sync_token(current) != expected_token
        ):
            return None, False
        metadata = dict(current.metadata)
        metadata.update(
            queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_REPAIR_PENDING,
                token=expected_token,
                owner_pid=0,
            )
        )
        entries[index] = replace(current, metadata=metadata)
        return None, True
    return None, False


def queue_record_publication_lock_path(root: str | Path, queue_id: str) -> Path:
    resolved_root = resolve_root_path(root)
    digest = sha256(str(queue_id).encode("utf-8")).hexdigest()
    return resolved_root / _QUEUE_RECORD_PUBLICATION_LOCK_DIR / f"{digest}.lock"


@contextmanager
def queue_record_publication_lock(
    root: str | Path,
    queue_id: str,
    *,
    timeout_seconds: float = QUEUE_RECORD_PUBLICATION_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serialize one entry's queued-record publication with cancellation.

    Callers may acquire the queue lock while holding this lock, but must never
    wait for this lock while still holding the queue lock.
    """
    lock_path = queue_record_publication_lock_path(root, queue_id)
    # The lock directory lives under an existing queue root; a missing root
    # stays missing rather than being created by a publication attempt.
    lock_path.parent.mkdir(exist_ok=True)
    with file_lock(lock_path, timeout_seconds=timeout_seconds):
        yield


def queue_entry_is_claimable(entry: Any) -> bool:
    # Only a committed publication makes a row claimable. Every other marker —
    # PREPARING, REPAIRING, REPAIR_PENDING, ABORTED, missing, and unknown —
    # requires the repair or cancellation path, which owns the lease under the
    # publication lock. Liveness of the recorded owner PID is deliberately not
    # consulted: the lock, not a live PID in the row, is the authoritative
    # ownership proof.
    return queue_record_sync_state(entry) == QUEUE_RECORD_SYNC_COMPLETE


__all__ = [
    "QUEUE_RECORD_PUBLICATION_LOCK_TIMEOUT_SECONDS",
    "QUEUE_RECORD_SYNC_ABORTED",
    "QUEUE_RECORD_SYNC_COMPLETE",
    "QUEUE_RECORD_SYNC_KEY",
    "QUEUE_RECORD_SYNC_OWNER_PID_KEY",
    "QUEUE_RECORD_SYNC_OWNER_START_KEY",
    "QUEUE_RECORD_SYNC_PREPARING",
    "QUEUE_RECORD_SYNC_REPAIR_PENDING",
    "QUEUE_RECORD_SYNC_REPAIRING",
    "QUEUE_RECORD_SYNC_TOKEN_KEY",
    "QUEUE_RECORD_SYNC_UPDATED_AT_KEY",
    "REPAIRABLE_SYNC_STATES",
    "park_queue_record_repair_pending",
    "process_start_token",
    "queue_entry_is_claimable",
    "queue_record_publication_lock",
    "queue_record_publication_lock_path",
    "queue_record_sync_metadata",
    "queue_record_sync_state",
    "queue_record_sync_token",
]
