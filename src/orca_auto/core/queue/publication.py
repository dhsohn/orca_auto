from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..utils.lock import file_lock
from ..utils.persistence import resolve_root_path

QUEUE_RECORD_SYNC_KEY = "_orca_auto_queued_record_sync"
QUEUE_RECORD_SYNC_UPDATED_AT_KEY = "_orca_auto_queued_record_sync_updated_at"
QUEUE_RECORD_SYNC_OWNER_PID_KEY = "_orca_auto_queued_record_sync_owner_pid"
QUEUE_RECORD_SYNC_OWNER_START_KEY = "_orca_auto_queued_record_sync_owner_start"
QUEUE_RECORD_SYNC_TOKEN_KEY = "_orca_auto_queued_record_sync_token"
# No producer since the workflow-level submitter cluster was removed; the
# constant survives only so the restart stale-key scrub can keep clearing the
# key out of durable stage metadata written by earlier releases.
QUEUE_SUBMISSION_INTENT_KEY = "submission_intent_token"

QUEUE_RECORD_SYNC_PREPARING = "preparing"
QUEUE_RECORD_SYNC_REPAIR_PENDING = "repair_pending"
QUEUE_RECORD_SYNC_REPAIRING = "repairing"
QUEUE_RECORD_SYNC_COMPLETE = "complete"
QUEUE_RECORD_SYNC_ABORTED = "aborted"

QUEUE_RECORD_PUBLICATION_LOCK_TIMEOUT_SECONDS = 300.0
_QUEUE_RECORD_PUBLICATION_LOCK_DIR = ".queue-publication-locks"


def queue_record_sync_state(entry: Any) -> str:
    metadata = getattr(entry, "metadata", {})
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get(QUEUE_RECORD_SYNC_KEY, "")).strip().lower()


def _linux_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def process_start_token(process_id: int) -> str:
    """Return a boot-scoped process-start identity when the OS exposes one."""
    if process_id <= 0:
        return ""
    try:
        stat_text = Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8")
    except OSError:
        return ""

    # The command field is parenthesized and may itself contain spaces or ')'.
    # Splitting after its final ')' makes index 19 the documented field 22,
    # starttime (clock ticks since boot).
    _prefix, separator, fields_text = stat_text.rpartition(")")
    if not separator:
        return ""
    fields = fields_text.strip().split()
    if len(fields) <= 19:
        return ""
    start_ticks = fields[19].strip()
    if not start_ticks:
        return ""
    boot_id = _linux_boot_id()
    return f"{boot_id}:{start_ticks}" if boot_id else ""


def current_process_start_token() -> str:
    return process_start_token(os.getpid())


def queue_record_sync_metadata(
    sync_state: str,
    *,
    token: str,
    owner_pid: int,
) -> dict[str, Any]:
    """Build the canonical fencing metadata for one publication lease."""
    normalized_owner_pid = int(owner_pid)
    return {
        QUEUE_RECORD_SYNC_KEY: str(sync_state).strip().lower(),
        QUEUE_RECORD_SYNC_UPDATED_AT_KEY: datetime.now(UTC).isoformat(),
        QUEUE_RECORD_SYNC_OWNER_PID_KEY: normalized_owner_pid,
        QUEUE_RECORD_SYNC_OWNER_START_KEY: (
            process_start_token(normalized_owner_pid) if normalized_owner_pid > 0 else ""
        ),
        QUEUE_RECORD_SYNC_TOKEN_KEY: str(token).strip(),
    }


def _publication_lock_path(root: str | Path, queue_id: str) -> Path:
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
    with file_lock(
        _publication_lock_path(root, queue_id),
        timeout_seconds=timeout_seconds,
    ):
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
    "QUEUE_SUBMISSION_INTENT_KEY",
    "current_process_start_token",
    "process_start_token",
    "queue_entry_is_claimable",
    "queue_record_publication_lock",
    "queue_record_sync_metadata",
    "queue_record_sync_state",
]
