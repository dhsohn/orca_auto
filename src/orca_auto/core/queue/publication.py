from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..utils.lock import file_lock
from ..utils.persistence import parse_iso_utc, resolve_root_path

QUEUE_RECORD_SYNC_KEY = "_orca_auto_queued_record_sync"
QUEUE_RECORD_SYNC_UPDATED_AT_KEY = "_orca_auto_queued_record_sync_updated_at"
QUEUE_RECORD_SYNC_OWNER_PID_KEY = "_orca_auto_queued_record_sync_owner_pid"
QUEUE_RECORD_SYNC_OWNER_START_KEY = "_orca_auto_queued_record_sync_owner_start"
QUEUE_RECORD_SYNC_TOKEN_KEY = "_orca_auto_queued_record_sync_token"

QUEUE_RECORD_SYNC_PREPARING = "preparing"
QUEUE_RECORD_SYNC_REPAIR_PENDING = "repair_pending"
QUEUE_RECORD_SYNC_REPAIRING = "repairing"
QUEUE_RECORD_SYNC_COMPLETE = "complete"
QUEUE_RECORD_SYNC_ABORTED = "aborted"

# New publishers record a PID plus process-start identity. A matching live
# owner must never be fenced merely because wall-clock time elapsed: it may be
# paused inside a slow filesystem or notification call. This timeout is only a
# compatibility escape hatch for old ownerless transient records.
QUEUE_RECORD_SYNC_HARD_STALE_SECONDS = 300.0
QUEUE_RECORD_PUBLICATION_LOCK_TIMEOUT_SECONDS = 300.0
_QUEUE_RECORD_PUBLICATION_LOCK_DIR = ".queue-publication-locks"

_UNCLAIMABLE_SYNC_STATES = frozenset(
    {
        QUEUE_RECORD_SYNC_PREPARING,
        QUEUE_RECORD_SYNC_REPAIRING,
    }
)


def queue_record_sync_state(entry: Any) -> str:
    metadata = getattr(entry, "metadata", {})
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get(QUEUE_RECORD_SYNC_KEY, "")).strip().lower()


def _owner_process_alive(owner_pid: int) -> bool:
    if owner_pid <= 0:
        return False
    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    try:
        stat_text = Path(f"/proc/{owner_pid}/stat").read_text(encoding="utf-8")
    except OSError:
        # Non-Linux platforms (or an unreadable procfs) retain the safe-biased
        # signal probe result.
        return True
    _prefix, separator, fields_text = stat_text.rpartition(")")
    if not separator:
        return True
    fields = fields_text.strip().split()
    # A zombie still answers ``kill(pid, 0)`` but cannot resume publication.
    # Treat it as dead so its PREPARING/REPAIRING lease cannot strand the job
    # until its parent eventually reaps it.
    return not fields or fields[0] != "Z"


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
    return f"{boot_id}:{start_ticks}" if boot_id else start_ticks


def current_process_start_token() -> str:
    return process_start_token(os.getpid())


def _same_process_start(recorded: str, current: str) -> bool:
    recorded_boot, recorded_separator, recorded_ticks = recorded.rpartition(":")
    current_boot, current_separator, current_ticks = current.rpartition(":")
    if not recorded_separator:
        recorded_boot, recorded_ticks = "", recorded
    if not current_separator:
        current_boot, current_ticks = "", current
    if recorded_ticks != current_ticks:
        return False
    # If one observer could not read boot_id, matching start ticks are the
    # safe-biased result. When both boot IDs are known they also fence reboots.
    return not (recorded_boot and current_boot) or recorded_boot == current_boot


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


def _sync_updated_at(entry: Any) -> datetime | None:
    metadata = getattr(entry, "metadata", {})
    if isinstance(metadata, dict):
        updated_at = parse_iso_utc(metadata.get(QUEUE_RECORD_SYNC_UPDATED_AT_KEY))
        if updated_at is not None:
            return updated_at
    return parse_iso_utc(getattr(entry, "enqueued_at", ""))


def queue_record_sync_is_stale(
    entry: Any,
    *,
    now: datetime | None = None,
    hard_stale_seconds: float = QUEUE_RECORD_SYNC_HARD_STALE_SECONDS,
) -> bool:
    metadata = getattr(entry, "metadata", {})
    owner_pid = 0
    owner_start = ""
    if isinstance(metadata, dict):
        try:
            owner_pid = int(metadata.get(QUEUE_RECORD_SYNC_OWNER_PID_KEY, 0) or 0)
        except (TypeError, ValueError):
            owner_pid = 0
        owner_start = str(metadata.get(QUEUE_RECORD_SYNC_OWNER_START_KEY, "") or "").strip()

    if owner_pid > 0:
        if not _owner_process_alive(owner_pid):
            return True
        current_owner_start = process_start_token(owner_pid)
        if (
            owner_start
            and current_owner_start
            and not _same_process_start(owner_start, current_owner_start)
        ):
            # The numeric PID was reused after the original publisher exited.
            return True
        # A matching live owner (or a platform where start identity cannot be
        # observed) keeps ownership regardless of timestamp age.
        return False

    updated_at = _sync_updated_at(entry)
    if updated_at is None:
        # Invalid/missing lease metadata must not strand a queue entry forever.
        return True

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)
    elapsed_seconds = (current - updated_at).total_seconds()
    if elapsed_seconds < -hard_stale_seconds:
        # A wildly future timestamp is invalid rather than an infinite lease.
        return True
    return elapsed_seconds >= hard_stale_seconds


def queue_entry_is_claimable(entry: Any) -> bool:
    state = queue_record_sync_state(entry)
    return state not in _UNCLAIMABLE_SYNC_STATES or queue_record_sync_is_stale(entry)


__all__ = [
    "QUEUE_RECORD_PUBLICATION_LOCK_TIMEOUT_SECONDS",
    "QUEUE_RECORD_SYNC_ABORTED",
    "QUEUE_RECORD_SYNC_COMPLETE",
    "QUEUE_RECORD_SYNC_HARD_STALE_SECONDS",
    "QUEUE_RECORD_SYNC_KEY",
    "QUEUE_RECORD_SYNC_OWNER_PID_KEY",
    "QUEUE_RECORD_SYNC_OWNER_START_KEY",
    "QUEUE_RECORD_SYNC_PREPARING",
    "QUEUE_RECORD_SYNC_REPAIR_PENDING",
    "QUEUE_RECORD_SYNC_REPAIRING",
    "QUEUE_RECORD_SYNC_TOKEN_KEY",
    "QUEUE_RECORD_SYNC_UPDATED_AT_KEY",
    "current_process_start_token",
    "process_start_token",
    "queue_entry_is_claimable",
    "queue_record_publication_lock",
    "queue_record_sync_is_stale",
    "queue_record_sync_state",
]
