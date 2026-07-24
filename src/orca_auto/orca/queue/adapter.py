"""Small ORCA adapter over the shared queue primitives."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from orca_auto.core.engines import entry_matches_engine_identity
from orca_auto.core.queue import store as _queue_store
from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_OWNER_START_KEY,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
)
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils.persistence import now_utc_iso, timestamped_token

from .entries import (
    ACTIVE_STATUSES,
    QUEUE_APP_NAME,
    QUEUE_ENGINE,
    QUEUE_FILE_NAME,
    QUEUE_TASK_KIND,
    TERMINAL_STATUSES,
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
from .entries import (
    load_entries as _load_entries,
)
from .orphans import reconcile_orphaned_running_entries
from .terminal_replay import (
    TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY,
    TERMINAL_REPLAY_METADATA_KEY,
    TerminalReplayMarkerKind,
    terminal_replay_is_fence_only,
    terminal_replay_marker_for_entry,
    terminal_replay_marker_kind,
)

logger = logging.getLogger(__name__)

_TOKEN_COLLISION_RETRY_LIMIT = 32

__all__ = [
    "ACTIVE_STATUSES",
    "DuplicateEntryError",
    "AmbiguousQueueTargetError",
    "QUEUE_APP_NAME",
    "QUEUE_ENGINE",
    "QUEUE_FILE_NAME",
    "QUEUE_TASK_KIND",
    "TERMINAL_STATUSES",
    "TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY",
    "TERMINAL_REPLAY_METADATA_KEY",
    "cancel",
    "clear_terminal",
    "dequeue_entry_if_pending",
    "dequeue_next",
    "enqueue",
    "get_active_entry_for_reaction_dir",
    "get_entry_by_id",
    "get_cancel_requested",
    "is_orca_queue_entry",
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
    "queue_entries_same_publication_generation",
    "reconcile_orphaned_running_entries",
    "requeue_running_entry",
    "update_terminal",
    "worker_log_path",
]


def _now_iso() -> str:
    return now_utc_iso()


def worker_log_path(allowed_root: Path, queue_id: str) -> Path:
    if (
        queue_id in {".", ".."}
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", queue_id) is None
    ):
        raise ValueError("Queue id is not safe for a worker log filename")
    return Path(allowed_root).expanduser().resolve() / "logs" / f"{queue_id}.log"


def _has_pending_terminal_replay(entry: QueueEntry) -> bool:
    # Unsupported future versions and malformed markers remain a conservative
    # clear/force barrier.  The worker admits only VALID markers and logs the
    # others as repair-blocked, so an older process cannot erase newer pending
    # side effects merely because it cannot decode their evidence.
    return terminal_replay_marker_kind(entry) is not TerminalReplayMarkerKind.ABSENT


def _terminal_metadata_update_fn(
    *,
    status: QueueStatus,
    error: str,
    metadata_update: Mapping[str, Any] | None = None,
    allow_terminal_candidate: bool = False,
) -> Callable[[QueueEntry], Mapping[str, Any] | None]:
    supplied_metadata = dict(metadata_update or {})

    def update(current: QueueEntry) -> Mapping[str, Any] | None:
        if supplied_metadata.get(TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY) is True:
            raise ValueError(
                "terminal side-effect replay and an administrative fence are mutually exclusive"
            )
        if not allow_terminal_candidate and current.status not in {
            QueueStatus.PENDING,
            QueueStatus.RUNNING,
        }:
            # Core terminal marks permit idempotent same-status calls.  Once a
            # completed marker has been cleared, such a call must not resurrect
            # replay work for closed history.  Explicit metadata still follows
            # the caller's request through the static update path.
            return None
        candidate_metadata = dict(current.metadata)
        candidate_metadata.update(supplied_metadata)
        candidate = replace(current, metadata=candidate_metadata)
        return {
            TERMINAL_REPLAY_METADATA_KEY: terminal_replay_marker_for_entry(
                candidate,
                status=status.value,
                error=error,
            )
        }

    return update


def _administrative_terminal_metadata_update_fn(
    current: QueueEntry,
) -> Mapping[str, Any]:
    if terminal_replay_marker_kind(current) is not TerminalReplayMarkerKind.ABSENT:
        raise ValueError(
            "an administrative terminal fence cannot replace pending side-effect replay"
        )
    return {TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY: True}


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
            "Wait for the active generation or its terminal publication to finish first."
        )


class AmbiguousQueueTargetError(ValueError):
    """Raised when a cancel alias names multiple active ORCA generations."""


def is_orca_queue_entry(entry: Any) -> bool:
    """Return whether a row carries the complete canonical ORCA identity."""
    return entry_matches_engine_identity(entry, QUEUE_ENGINE)


def _reject_duplicate_reaction_dir(
    entries: Sequence[QueueEntry],
    entry: QueueEntry,
) -> None:
    entry_key = queue_entry_reaction_dir(entry)
    owned_entries = [existing for existing in entries if is_orca_queue_entry(existing)]
    for existing in owned_entries:
        if (
            queue_entry_reaction_dir(existing) == entry_key
            and queue_entry_status(existing) in TERMINAL_STATUSES
            and (_has_pending_terminal_replay(existing) or terminal_replay_is_fence_only(existing))
        ):
            # A terminal queue mark is only the first half of publication.  Until
            # its durable replay marker is cleared, the parent still owns this
            # reaction-dir generation and a forced successor would make its
            # state/index/notification side effects impossible to replay safely.
            raise DuplicateEntryError(entry_key, existing)
    _queue_store.reject_duplicate_entry_key(
        owned_entries,
        key=entry_key,
        key_fn=queue_entry_reaction_dir,
        # A closed generation owns its visible execution directory, not the
        # public source folder. Only an active row (or the replay fence above)
        # blocks a new sibling generation in the same job directory.
        force=True,
        active_statuses=ACTIVE_STATUSES,
        terminal_statuses=TERMINAL_STATUSES,
        error_factory=DuplicateEntryError,
    )


def _unique_timestamped_token(
    prefix: str,
    *,
    occupied: set[str],
) -> str:
    for _attempt in range(_TOKEN_COLLISION_RETRY_LIMIT):
        candidate = timestamped_token(prefix)
        if candidate not in occupied:
            return candidate
    raise RuntimeError(
        f"Could not allocate a unique {prefix} token after {_TOKEN_COLLISION_RETRY_LIMIT} attempts"
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
    before_commit_fn: Callable[[], Any] | None = None,
    after_commit_fn: Callable[[], Any] | None = None,
) -> QueueEntry:
    """Add a reaction directory to the ORCA queue."""
    resolved = str(Path(reaction_dir).expanduser().resolve())
    reconcile_orphaned_running_entries(allowed_root)
    normalized_priority = normalize_queue_priority(priority)
    normalized_task_id = normalize_text(task_id)
    normalized_task_kind = normalize_text(task_kind) or QUEUE_TASK_KIND

    def append(entries: list[QueueEntry]) -> tuple[QueueEntry, bool]:
        queue_id = _unique_timestamped_token(
            "q",
            occupied={entry.queue_id for entry in entries},
        )
        resolved_task_id = normalized_task_id or _unique_timestamped_token(
            "orca",
            occupied={normalize_text(entry.task_id) for entry in entries},
        )
        queue_metadata = entry_metadata(
            reaction_dir=resolved,
            force=force,
            extra=metadata,
        )
        queue_metadata["worker_log"] = str(worker_log_path(allowed_root, queue_id))
        entry = QueueEntry(
            queue_id=queue_id,
            app_name=QUEUE_APP_NAME,
            task_id=resolved_task_id,
            task_kind=normalized_task_kind,
            engine=QUEUE_ENGINE,
            status=QueueStatus.PENDING,
            priority=normalized_priority,
            enqueued_at=_now_iso(),
            metadata=queue_metadata,
        )
        _reject_duplicate_reaction_dir(entries, entry)
        if before_commit_fn is not None:
            before_commit_fn()
        entries.append(entry)
        return entry, True

    entry = _queue_store.mutate_entries(
        allowed_root,
        append,
        load_entries_fn=_load_entries,
        save_entries_fn=_queue_store.save_entries,
        after_commit_fn=after_commit_fn,
    )
    logger.info("Enqueued: %s (queue_id=%s, force=%s)", resolved, entry.queue_id, force)
    return entry


def queue_entries_same_publication_generation(current: QueueEntry, expected: QueueEntry) -> bool:
    return bool(
        current.queue_id == expected.queue_id
        and current.app_name == expected.app_name
        and current.task_id == expected.task_id
        and current.task_kind == expected.task_kind
        and current.engine == expected.engine
        and current.priority == expected.priority
        and queue_entry_reaction_dir(current) == queue_entry_reaction_dir(expected)
        and queue_entry_force(current) is queue_entry_force(expected)
        and _immutable_publication_metadata(current.metadata)
        == _immutable_publication_metadata(expected.metadata)
    )


def _immutable_publication_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    lease_keys = {
        QUEUE_RECORD_SYNC_KEY,
        QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
        QUEUE_RECORD_SYNC_OWNER_PID_KEY,
        QUEUE_RECORD_SYNC_OWNER_START_KEY,
        QUEUE_RECORD_SYNC_TOKEN_KEY,
        TERMINAL_REPLAY_METADATA_KEY,
        "run_id",
    }
    return {key: value for key, value in metadata.items() if key not in lease_keys}


def dequeue_next(
    allowed_root: Path,
    *,
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
) -> QueueEntry | None:
    """Return the highest-priority pending entry and mark it running.

    ``accept_entry_fn`` scopes which entries this worker may claim. The ORCA
    worker shares the runs root with standalone xTB/CREST jobs, so it must pass
    an app filter (like the internal engines do) to avoid claiming a foreign
    engine's entry on the single-root dequeue fast path.
    """

    def accepts_orca(entry: QueueEntry) -> bool:
        return is_orca_queue_entry(entry) and (accept_entry_fn is None or accept_entry_fn(entry))

    entry = _queue_store.dequeue_next(
        allowed_root,
        load_entries_fn=_load_entries,
        save_entries_fn=_queue_store.save_entries,
        accept_entry_fn=accepts_orca,
    )
    if entry is None:
        return None
    logger.info(
        "Dequeued: %s (queue_id=%s)",
        queue_entry_reaction_dir(entry),
        queue_entry_id(entry),
    )
    return entry


def dequeue_entry_if_pending(
    allowed_root: Path,
    queue_id: str,
    *,
    expected_entry: QueueEntry | None = None,
) -> QueueEntry | None:
    """Mark a selected pending ORCA queue entry running if it is still eligible."""
    entry = _queue_store.dequeue_entry_if_pending(
        allowed_root,
        queue_id,
        load_entries_fn=_load_entries,
        save_entries_fn=_queue_store.save_entries,
        accept_entry_fn=is_orca_queue_entry,
        expected_entry=expected_entry,
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
    """Return the unique active ORCA generation for a cancel target.

    Path and run aliases may remain on older terminal generations.  An active
    generation takes precedence; multiple active matches are ambiguous and
    must not be changed.  With no active match, return the newest terminal row
    so cancellation retries can observe an already-cancelled outcome.
    """

    matches = [
        entry
        for entry in entries
        if is_orca_queue_entry(entry) and queue_entry_matches_target(entry, target)
    ]
    active = [entry for entry in matches if queue_entry_status(entry) in ACTIVE_STATUSES]
    if len(active) > 1:
        raise AmbiguousQueueTargetError(
            f"queue target matches multiple active ORCA generations: {target}"
        )
    if active:
        return active[0]
    if not matches:
        return None
    return max(
        matches,
        key=lambda entry: (
            normalize_text(entry.finished_at),
            normalize_text(entry.started_at),
            normalize_text(entry.enqueued_at),
            queue_entry_id(entry),
        ),
    )


def mark_completed(
    allowed_root: Path,
    queue_id: str,
    *,
    run_id: str | None = None,
    metadata_update: dict[str, Any] | None = None,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
) -> bool:
    """Mark a queue entry as completed."""
    merged_metadata = dict(metadata_update or {})
    if run_id is not None:
        merged_metadata["run_id"] = run_id
    return (
        _queue_store.mark_completed(
            allowed_root,
            queue_id,
            metadata_update=merged_metadata or None,
            metadata_update_fn=_terminal_metadata_update_fn(
                status=QueueStatus.COMPLETED,
                error="",
                metadata_update=merged_metadata,
            ),
            load_entries_fn=_load_entries,
            save_entries_fn=_queue_store.save_entries,
            accept_entry_fn=lambda current: (
                is_orca_queue_entry(current)
                and (
                    expected_entry is None
                    or queue_entries_same_publication_generation(current, expected_entry)
                )
                and (
                    expected_task_id is None
                    or normalize_text(current.task_id) == normalize_text(expected_task_id)
                )
            ),
        )
        is not None
    )


def mark_failed(
    allowed_root: Path,
    queue_id: str,
    *,
    error: str | None = None,
    run_id: str | None = None,
    metadata_update: dict[str, Any] | None = None,
    publish_terminal_side_effects: bool = True,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
) -> bool:
    """Mark a queue entry as failed."""
    normalized_error = error or ""
    merged_metadata = dict(metadata_update or {})
    if not publish_terminal_side_effects:
        if merged_metadata.get(TERMINAL_REPLAY_METADATA_KEY) is not None:
            raise ValueError(
                "an administrative terminal fence cannot carry a side-effect replay marker"
            )
        merged_metadata[TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY] = True
    if run_id is not None:
        merged_metadata["run_id"] = run_id
    return (
        _queue_store.mark_failed(
            allowed_root,
            queue_id,
            error=normalized_error,
            metadata_update=merged_metadata or None,
            metadata_update_fn=(
                _terminal_metadata_update_fn(
                    status=QueueStatus.FAILED,
                    error=normalized_error,
                    metadata_update=merged_metadata,
                )
                if publish_terminal_side_effects
                else _administrative_terminal_metadata_update_fn
            ),
            load_entries_fn=_load_entries,
            save_entries_fn=_queue_store.save_entries,
            accept_entry_fn=lambda current: (
                is_orca_queue_entry(current)
                and (
                    expected_entry is None
                    or queue_entries_same_publication_generation(current, expected_entry)
                )
                and (
                    expected_task_id is None
                    or normalize_text(current.task_id) == normalize_text(expected_task_id)
                )
            ),
        )
        is not None
    )


def mark_cancelled(
    allowed_root: Path,
    queue_id: str,
    *,
    metadata_update: dict[str, Any] | None = None,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
) -> bool:
    """Mark a running queue entry as cancelled after the worker stops it."""
    return (
        _queue_store.mark_cancelled(
            allowed_root,
            queue_id,
            error="",
            metadata_update=metadata_update,
            metadata_update_fn=_terminal_metadata_update_fn(
                status=QueueStatus.CANCELLED,
                error="cancel_requested",
                metadata_update=metadata_update,
            ),
            load_entries_fn=_load_entries,
            save_entries_fn=_queue_store.save_entries,
            accept_entry_fn=lambda current: (
                is_orca_queue_entry(current)
                and current.status == QueueStatus.RUNNING
                and (
                    expected_entry is None
                    or queue_entries_same_publication_generation(current, expected_entry)
                )
                and (
                    expected_task_id is None
                    or normalize_text(current.task_id) == normalize_text(expected_task_id)
                )
            ),
        )
        is not None
    )


def requeue_running_entry(
    allowed_root: Path,
    queue_id: str,
    *,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
) -> bool:
    """Return a running queue entry back to pending during worker shutdown.

    If a cancel was requested for the entry, it is marked cancelled instead of
    requeued so a cancelled job is not resumed (see core queue store).
    """
    return (
        _queue_store.requeue_running_entry(
            allowed_root,
            queue_id,
            cancel_metadata_update_fn=_terminal_metadata_update_fn(
                status=QueueStatus.CANCELLED,
                error="cancel_requested",
                allow_terminal_candidate=True,
            ),
            load_entries_fn=_load_entries,
            save_entries_fn=_queue_store.save_entries,
            accept_entry_fn=lambda current: (
                is_orca_queue_entry(current)
                and (
                    expected_entry is None
                    or queue_entries_same_publication_generation(current, expected_entry)
                )
                and (
                    expected_task_id is None
                    or normalize_text(current.task_id) == normalize_text(expected_task_id)
                )
            ),
        )
        is not None
    )


def cancel(
    allowed_root: Path,
    queue_id: str,
    *,
    expected_entry: QueueEntry | None = None,
) -> QueueEntry | None:
    """Cancel a queue entry."""
    entry = _queue_store.request_cancel(
        allowed_root,
        queue_id,
        pending_metadata_update_fn=_terminal_metadata_update_fn(
            status=QueueStatus.CANCELLED,
            error="cancel_requested",
            allow_terminal_candidate=True,
        ),
        accept_entry_fn=lambda current: (
            is_orca_queue_entry(current)
            and (
                expected_entry is None
                or queue_entries_same_publication_generation(current, expected_entry)
            )
        ),
        load_entries_fn=_load_entries,
        save_entries_fn=_queue_store.save_entries,
    )
    if entry is None:
        logger.debug("Cannot cancel missing or terminal entry: %s", queue_id)
    elif entry.status == QueueStatus.CANCELLED:
        logger.info("Cancelled pending entry: %s", queue_id)
    else:
        logger.info("Cancel requested for running entry: %s", queue_id)
    return entry


def list_queue(
    allowed_root: Path,
    *,
    status_filter: str | None = None,
) -> list[QueueEntry]:
    """List queue entries, optionally filtered by status."""
    entries = [
        entry
        for entry in _queue_store.list_queue(allowed_root, load_entries_fn=_load_entries)
        if is_orca_queue_entry(entry)
    ]
    if status_filter:
        normalized_filter = normalize_text(status_filter).lower()
        entries = [e for e in entries if queue_entry_status(e) == normalized_filter]
    return entries


def get_entry_by_id(allowed_root: Path, queue_id: str) -> QueueEntry | None:
    """Return one queue entry without changing its state."""
    return next((entry for entry in list_queue(allowed_root) if entry.queue_id == queue_id), None)


def get_active_entry_for_reaction_dir(allowed_root: Path, reaction_dir: str) -> QueueEntry | None:
    """Return the active queue entry for a reaction_dir, if one exists."""
    resolved = str(Path(reaction_dir).expanduser().resolve())
    return find_active_entry(list_queue(allowed_root), resolved)


def get_cancel_requested(
    allowed_root: Path,
    queue_id: str,
    *,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
) -> bool:
    """Check if a running entry has a cancel request."""
    return _queue_store.get_cancel_requested(
        allowed_root,
        queue_id,
        load_entries_fn=_load_entries,
        accept_entry_fn=lambda current: (
            is_orca_queue_entry(current)
            and (
                expected_entry is None
                or queue_entries_same_publication_generation(current, expected_entry)
            )
            and (
                expected_task_id is None
                or normalize_text(current.task_id) == normalize_text(expected_task_id)
            )
        ),
    )


def clear_terminal(allowed_root: Path, *, keep_last: int = 0) -> int:
    """Remove completed/failed/cancelled entries. Returns count removed."""
    removed_count = _queue_store.clear_terminal(
        allowed_root,
        keep_last=keep_last,
        retain_entry_fn=_has_pending_terminal_replay,
        select_entry_fn=is_orca_queue_entry,
        load_entries_fn=_load_entries,
        save_entries_fn=_queue_store.save_entries,
    )
    logger.info("Cleared %d terminal entries", removed_count)
    return removed_count


def update_metadata(
    allowed_root: Path,
    queue_id: str,
    metadata_update: dict[str, Any],
    *,
    expected_entry: QueueEntry | None = None,
) -> bool:
    return (
        _queue_store.update_metadata(
            allowed_root,
            queue_id,
            metadata_update,
            load_entries_fn=_load_entries,
            save_entries_fn=_queue_store.save_entries,
            accept_entry_fn=lambda current: (
                is_orca_queue_entry(current)
                and (
                    expected_entry is None
                    or queue_entries_same_publication_generation(current, expected_entry)
                )
            ),
        )
        is not None
    )


def update_terminal(
    allowed_root: Path,
    queue_id: str,
    status: str,
    *,
    error: str | None = None,
    run_id: str | None = None,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
) -> bool:
    target_status = normalize_text(status).lower()
    if target_status not in TERMINAL_STATUSES:
        return False

    def update(current: QueueEntry) -> tuple[bool, QueueEntry | None]:
        if not is_orca_queue_entry(current):
            return False, None
        if current.status not in {
            QueueStatus.COMPLETED,
            QueueStatus.FAILED,
            QueueStatus.CANCELLED,
        }:
            # Recovery-only correction.  Active -> terminal transitions must
            # use the canonical mark/cancel APIs so their replay marker is part
            # of the same durable queue write.
            return False, None
        if expected_entry is not None and not queue_entries_same_publication_generation(
            current, expected_entry
        ):
            return False, None
        if expected_task_id is not None and normalize_text(current.task_id) != normalize_text(
            expected_task_id
        ):
            return False, None
        metadata = dict(current.metadata)
        if run_id is not None:
            metadata["run_id"] = run_id
        entry = replace(
            current,
            status=QueueStatus(target_status),
            finished_at=_now_iso(),
            error=error if error is not None else current.error,
            metadata=metadata,
        )
        logger.info("Entry %s -> %s", queue_id, target_status)
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
