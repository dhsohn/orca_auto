"""Small ORCA adapter over the shared queue primitives."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from orca_auto.core.queue import store as _queue_store
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils.persistence import now_utc_iso, timestamped_token

from .entries import (
    ACTIVE_STATUSES,
    QUEUE_APP_NAME,
    QUEUE_ENGINE,
    QUEUE_FILE_NAME,
    QUEUE_TASK_KIND,
    TERMINAL_STATUSES,
    entry_from_json_payload,
    entry_metadata,
    find_active_entry,
    normalize_text,
    queue_entry_app_name,
    queue_entry_force,
    queue_entry_id,
    queue_entry_matches_target,
    queue_entry_metadata,
    queue_entry_priority,
    queue_entry_reaction_dir,
    queue_entry_run_id,
    queue_entry_status,
    queue_entry_task_id,
)
from .orphans import reconcile_orphaned_running_entries

logger = logging.getLogger(__name__)

_UNSET = object()

__all__ = [
    "ACTIVE_STATUSES",
    "DuplicateEntryError",
    "QUEUE_APP_NAME",
    "QUEUE_ENGINE",
    "QUEUE_FILE_NAME",
    "QUEUE_TASK_KIND",
    "TERMINAL_STATUSES",
    "cancel",
    "cancel_pending_entry",
    "clear_terminal",
    "dequeue_entry_if_pending",
    "dequeue_next",
    "enqueue",
    "get_active_entry_for_reaction_dir",
    "get_cancel_requested",
    "has_pending_entries",
    "find_entry_by_target",
    "list_queue",
    "mark_cancelled",
    "mark_completed",
    "mark_failed",
    "queue_entry_matches_target",
    "queue_entry_app_name",
    "queue_entry_force",
    "queue_entry_id",
    "queue_entry_metadata",
    "queue_entry_priority",
    "queue_entry_reaction_dir",
    "queue_entry_run_id",
    "queue_entry_status",
    "queue_entry_task_id",
    "reconcile_orphaned_running_entries",
    "requeue_running_entry",
    "update_running_entry_state",
    "update_terminal",
    "worker_log_path",
]


def _now_iso() -> str:
    return now_utc_iso()


def worker_log_path(allowed_root: Path, queue_id: str) -> Path:
    return Path(allowed_root).expanduser().resolve() / "logs" / f"{queue_id}.log"


def _load_entries(allowed_root: Path) -> list[QueueEntry]:
    return _queue_store.load_entries(
        allowed_root,
        entry_from_dict_fn=entry_from_json_payload,
        corrupt_error=_queue_store.QueueStoreCorruptError,
    )


class DuplicateEntryError(ValueError):
    """Raised when enqueueing a reaction_dir that already has an active entry."""

    def __init__(
        self,
        reaction_dir: str,
        existing: QueueEntry,
    ) -> None:
        self.existing = existing
        status = queue_entry_status(self.existing) or "?"
        qid = queue_entry_id(self.existing) or "?"
        super().__init__(
            f"Reaction directory already queued: {reaction_dir} "
            f"(queue_id={qid}, status={status}). "
            f"Use --force to re-enqueue a completed/failed job, or cancel the existing entry first."
        )


def _reject_duplicate_reaction_dir(
    entries: Sequence[QueueEntry],
    entry: QueueEntry,
) -> None:
    _queue_store.reject_duplicate_entry_key(
        entries,
        key=queue_entry_reaction_dir(entry),
        key_fn=queue_entry_reaction_dir,
        force=queue_entry_force(entry),
        active_statuses=ACTIVE_STATUSES,
        terminal_statuses=TERMINAL_STATUSES,
        error_factory=DuplicateEntryError,
    )


def enqueue(
    allowed_root: Path,
    reaction_dir: str,
    *,
    priority: int = 10,
    force: bool = False,
    task_id: str | None = None,
    task_kind: str = QUEUE_TASK_KIND,
    metadata: dict[str, Any] | None = None,
) -> QueueEntry:
    """Add a reaction directory to the ORCA queue."""
    resolved = str(Path(reaction_dir).expanduser().resolve())
    reconcile_orphaned_running_entries(allowed_root)
    queue_id = timestamped_token("q", token_bytes=4)
    queue_metadata = entry_metadata(
        reaction_dir=resolved,
        force=force,
        extra=metadata,
    )
    queue_metadata["worker_log"] = str(worker_log_path(allowed_root, queue_id))

    entry = QueueEntry(
        queue_id=queue_id,
        app_name=QUEUE_APP_NAME,
        task_id=normalize_text(task_id) or timestamped_token("orca", token_bytes=4),
        task_kind=normalize_text(task_kind) or QUEUE_TASK_KIND,
        engine=QUEUE_ENGINE,
        status=QueueStatus.PENDING,
        priority=priority,
        enqueued_at=_now_iso(),
        metadata=queue_metadata,
    )
    entry = _queue_store.enqueue_entry(
        allowed_root,
        entry,
        duplicate_policy=_reject_duplicate_reaction_dir,
        load_entries_fn=_load_entries,
        save_entries_fn=_queue_store.save_entries,
    )
    logger.info("Enqueued: %s (queue_id=%s, force=%s)", resolved, entry.queue_id, force)
    return entry


def dequeue_next(allowed_root: Path) -> QueueEntry | None:
    """Return the highest-priority pending entry and mark it running."""
    entry = _queue_store.dequeue_next(
        allowed_root,
        load_entries_fn=_load_entries,
        save_entries_fn=_queue_store.save_entries,
    )
    if entry is None:
        return None
    logger.info(
        "Dequeued: %s (queue_id=%s)",
        queue_entry_reaction_dir(entry),
        queue_entry_id(entry),
    )
    return entry


def dequeue_entry_if_pending(allowed_root: Path, queue_id: str) -> QueueEntry | None:
    """Mark a selected pending ORCA queue entry running if it is still eligible."""
    entry = _queue_store.dequeue_entry_if_pending(
        allowed_root,
        queue_id,
        load_entries_fn=_load_entries,
        save_entries_fn=_queue_store.save_entries,
    )
    if entry is None:
        return None
    logger.info(
        "Dequeued: %s (queue_id=%s)",
        queue_entry_reaction_dir(entry),
        queue_entry_id(entry),
    )
    return entry


def find_entry_by_target(entries: Sequence[QueueEntry], target: str) -> QueueEntry | None:
    """Return the first entry matching a user-facing queue cancel target."""

    for entry in entries:
        if queue_entry_matches_target(entry, target):
            return entry
    return None


def mark_completed(allowed_root: Path, queue_id: str, *, run_id: str | None = None) -> bool:
    """Mark a queue entry as completed."""
    metadata_update = {"run_id": run_id} if run_id is not None else None
    return (
        _queue_store.mark_completed(
            allowed_root,
            queue_id,
            metadata_update=metadata_update,
            load_entries_fn=_load_entries,
            save_entries_fn=_queue_store.save_entries,
        )
        is not None
    )


def mark_failed(
    allowed_root: Path,
    queue_id: str,
    *,
    error: str | None = None,
    run_id: str | None = None,
) -> bool:
    """Mark a queue entry as failed."""
    metadata_update = {"run_id": run_id} if run_id is not None else None
    return (
        _queue_store.mark_failed(
            allowed_root,
            queue_id,
            error=error or "",
            metadata_update=metadata_update,
            load_entries_fn=_load_entries,
            save_entries_fn=_queue_store.save_entries,
        )
        is not None
    )


def mark_cancelled(allowed_root: Path, queue_id: str) -> bool:
    """Mark a running queue entry as cancelled after the worker stops it."""
    return update_running_entry_state(
        allowed_root,
        queue_id,
        status=QueueStatus.CANCELLED.value,
        finished_at=_now_iso(),
        cancel_requested=False,
    )


def requeue_running_entry(allowed_root: Path, queue_id: str) -> bool:
    """Return a running queue entry back to pending during worker shutdown.

    If a cancel was requested for the entry, it is marked cancelled instead of
    requeued so a cancelled job is not resumed (see core queue store).
    """
    return (
        _queue_store.requeue_running_entry(
            allowed_root,
            queue_id,
            load_entries_fn=_load_entries,
            save_entries_fn=_queue_store.save_entries,
        )
        is not None
    )


def cancel(allowed_root: Path, queue_id: str) -> QueueEntry | None:
    """Cancel a queue entry."""

    def cancel_entry(current: QueueEntry) -> tuple[QueueEntry | None, QueueEntry | None]:
        if current.status == QueueStatus.PENDING:
            entry = cancel_pending_entry(current, finished_at=_now_iso())
            logger.info("Cancelled pending entry: %s", queue_id)
            return entry, entry
        if current.status == QueueStatus.RUNNING:
            entry = replace(current, cancel_requested=True)
            logger.info("Cancel requested for running entry: %s", queue_id)
            return entry, entry

        logger.debug(
            "Cannot cancel entry in terminal state: %s (%s)",
            queue_id,
            current.status.value,
        )
        return None, None

    return cast(
        QueueEntry | None,
        _queue_store.mutate_entry_by_id(
            allowed_root,
            queue_id,
            cancel_entry,
            missing_result=None,
            load_entries_fn=_load_entries,
            save_entries_fn=_queue_store.save_entries,
        ),
    )


def list_queue(
    allowed_root: Path,
    *,
    status_filter: str | None = None,
) -> list[QueueEntry]:
    """List queue entries, optionally filtered by status."""
    entries = _queue_store.list_queue(allowed_root, load_entries_fn=_load_entries)
    if status_filter:
        normalized_filter = normalize_text(status_filter).lower()
        entries = [e for e in entries if queue_entry_status(e) == normalized_filter]
    return entries


def has_pending_entries(allowed_root: Path) -> bool:
    """Return True when at least one pending entry exists."""
    return any(entry.status == QueueStatus.PENDING for entry in list_queue(allowed_root))


def get_active_entry_for_reaction_dir(allowed_root: Path, reaction_dir: str) -> QueueEntry | None:
    """Return the active queue entry for a reaction_dir, if one exists."""
    resolved = str(Path(reaction_dir).expanduser().resolve())
    return find_active_entry(list_queue(allowed_root), resolved)


def get_cancel_requested(allowed_root: Path, queue_id: str) -> bool:
    """Check if a running entry has a cancel request."""
    return _queue_store.get_cancel_requested(
        allowed_root,
        queue_id,
        load_entries_fn=_load_entries,
    )


def clear_terminal(allowed_root: Path, *, keep_last: int = 0) -> int:
    """Remove completed/failed/cancelled entries. Returns count removed."""
    removed_count = _queue_store.clear_terminal(
        allowed_root,
        keep_last=keep_last,
        load_entries_fn=_load_entries,
        save_entries_fn=_queue_store.save_entries,
    )
    logger.info("Cleared %d terminal entries", removed_count)
    return removed_count


def update_terminal(
    allowed_root: Path,
    queue_id: str,
    status: str,
    *,
    error: str | None = None,
    run_id: str | None = None,
) -> bool:
    def update(current: QueueEntry) -> tuple[bool, QueueEntry]:
        metadata = dict(current.metadata)
        if run_id is not None:
            metadata["run_id"] = run_id
        entry = replace(
            current,
            status=QueueStatus(status),
            finished_at=_now_iso(),
            error=error if error is not None else current.error,
            metadata=metadata,
        )
        logger.info("Entry %s -> %s", queue_id, status)
        return True, entry

    return bool(
        _queue_store.mutate_entry_by_id(
            allowed_root,
            queue_id,
            update,
            missing_result=False,
            load_entries_fn=_load_entries,
            save_entries_fn=_queue_store.save_entries,
        )
    )


def cancel_pending_entry(entry: QueueEntry, *, finished_at: str) -> QueueEntry:
    return replace(entry, status=QueueStatus.CANCELLED, finished_at=finished_at)


def update_running_entry_state(
    allowed_root: Path,
    queue_id: str,
    *,
    status: str,
    started_at: object = _UNSET,
    finished_at: object = _UNSET,
    cancel_requested: bool | None = None,
) -> bool:
    def update(current: QueueEntry) -> tuple[bool, QueueEntry | None]:
        if current.status != QueueStatus.RUNNING:
            return False, None
        updates: dict[str, Any] = {"status": QueueStatus(status)}
        if started_at is not _UNSET:
            updates["started_at"] = cast(str | None, started_at) or ""
        if finished_at is not _UNSET:
            updates["finished_at"] = cast(str | None, finished_at) or ""
        if cancel_requested is not None:
            updates["cancel_requested"] = cancel_requested
        entry = replace(current, **updates)
        logger.info("Entry %s -> %s", queue_id, status)
        return True, entry

    return bool(
        _queue_store.mutate_entry_by_id(
            allowed_root,
            queue_id,
            update,
            missing_result=False,
            load_entries_fn=_load_entries,
            save_entries_fn=_queue_store.save_entries,
        )
    )
