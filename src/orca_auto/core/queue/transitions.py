"""Queue row lifecycle transitions: cancellation and terminal marks.

:mod:`.store` owns storage, locking, duplicate policy and the
enqueue/dequeue/clear operations. This module owns every transition that
ends or interrupts a row's life:

* :func:`terminal_entry` is the only constructor of COMPLETED/FAILED/
  CANCELLED rows.
* :func:`request_cancel` cancels a pending row outright (revoking any
  publication lease it holds) or flags a running row for its worker.
* :func:`mark_completed`, :func:`mark_failed` and :func:`mark_cancelled`
  move an active row to its terminal status under the queue lock.
* :func:`correct_terminal_status` is the recovery-only terminal -> terminal
  correction; :func:`requeue_running_entry` returns a running row to
  pending, or honours a pending cancellation instead.

Every writer goes through :class:`.store.QueueStore`, so lock discipline and
the on-disk format stay defined in one place.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..utils.persistence import now_utc_iso
from .generation import queue_entries_same_generation
from .publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_OWNER_START_KEY,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QUEUE_RECORD_SYNC_REPAIRING,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
    queue_record_publication_lock,
)
from .store import QueueStore, queue_lock
from .types import TERMINAL_QUEUE_STATUSES, QueueEntry, QueueStatus

_TERMINAL_STATUSES = TERMINAL_QUEUE_STATUSES

_MetadataUpdateFn = Callable[[QueueEntry], Mapping[str, Any] | None]
_AcceptEntryFn = Callable[[QueueEntry], bool]


def _merged_metadata(
    entry: QueueEntry,
    *,
    metadata_update: Mapping[str, Any] | None = None,
    metadata_update_fn: _MetadataUpdateFn | None = None,
) -> dict[str, Any]:
    merged = dict(entry.metadata)
    if metadata_update:
        merged.update(metadata_update)
    if metadata_update_fn is not None:
        generated_update = metadata_update_fn(entry)
        if generated_update is not None:
            if not isinstance(generated_update, Mapping):
                raise TypeError("metadata update callback must return a mapping or None")
            merged.update(generated_update)
    return merged


def terminal_entry(
    entry: QueueEntry,
    *,
    status: QueueStatus,
    error: str | None,
    finished_at: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    cancel_requested: bool | None = None,
) -> QueueEntry:
    """Build the terminal row for ``entry``.

    This is the only constructor of COMPLETED/FAILED/CANCELLED rows, so the
    terminal rules live in one place:

    * ``status`` must be terminal; anything else raises ``ValueError``.
    * ``finished_at`` defaults to now. An explicit value keeps an
      authoritative timestamp (an idempotent re-mark, an orphan's state file).
    * ``cancel_requested`` is cleared by a CANCELLED row (the request has been
      honored) and preserved otherwise. A caller that records the flag on
      purpose (a pending row cancelled on request, an ambiguous-identity
      fence) passes it explicitly.
    * ``error`` ``None`` keeps the row's current error; a string is stripped.
    * ``metadata`` ``None`` keeps the row's metadata; a mapping replaces it.
    """
    target = QueueStatus(status)
    if target not in _TERMINAL_STATUSES:
        raise ValueError(f"terminal_entry requires a terminal status, got {target.value!r}")
    if cancel_requested is None:
        cancel_requested = False if target == QueueStatus.CANCELLED else entry.cancel_requested
    return replace(
        entry,
        status=target,
        finished_at=now_utc_iso() if finished_at is None else finished_at,
        cancel_requested=cancel_requested,
        error=entry.error if error is None else error.strip(),
        metadata=entry.metadata if metadata is None else dict(metadata),
    )


# --- request_cancel -----------------------------------------------------------


def _cancel_accepted(
    entry: QueueEntry,
    *,
    accept_entry_fn: _AcceptEntryFn | None,
    expected_entry: QueueEntry | None,
) -> bool:
    """Whether ``entry`` is the row the caller asked to cancel."""
    if accept_entry_fn is not None and not accept_entry_fn(entry):
        return False
    if expected_entry is not None and not queue_entries_same_generation(
        entry,
        expected_entry,
    ):
        return False
    return True


def _revoked_publication_metadata(entry: QueueEntry, *, finished_at: str) -> dict[str, Any]:
    """Metadata for a pending row whose in-flight publication lease is revoked.

    A row with no live lease (COMPLETE, ABORTED, or unset) keeps its metadata.
    """
    metadata = dict(entry.metadata)
    sync_state = str(metadata.get(QUEUE_RECORD_SYNC_KEY, "")).strip().lower()
    if sync_state in {
        QUEUE_RECORD_SYNC_PREPARING,
        QUEUE_RECORD_SYNC_REPAIR_PENDING,
        QUEUE_RECORD_SYNC_REPAIRING,
    }:
        # Cancellation owns the per-entry publication lock here. Revoke
        # the publisher's fencing token before releasing it so a
        # publisher that had not started its side effects cannot resume.
        # ABORTED is a forward fence, not a cross-store rollback: a
        # process killed between state, index, and notification writes
        # may have left an outcome-unknown partial record. Terminal
        # reconciliation owns that artifact; cancellation guarantees
        # only that no publisher can add a late write after this point.
        metadata.update(
            {
                QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_ABORTED,
                QUEUE_RECORD_SYNC_UPDATED_AT_KEY: finished_at,
                QUEUE_RECORD_SYNC_OWNER_PID_KEY: 0,
                QUEUE_RECORD_SYNC_OWNER_START_KEY: "",
                QUEUE_RECORD_SYNC_TOKEN_KEY: "",
            }
        )
    return metadata


def _cancelled_pending_entry(
    entry: QueueEntry,
    *,
    finished_at: str,
    pending_metadata_update_fn: _MetadataUpdateFn | None,
) -> QueueEntry:
    """Build the CANCELLED row for a pending entry.

    The row keeps ``cancel_requested=True`` on purpose (a pending row cancelled
    on request), revokes any publication lease, and then merges the caller's
    metadata callback on top of the terminal row.
    """
    candidate = terminal_entry(
        entry,
        status=QueueStatus.CANCELLED,
        error=None,
        finished_at=finished_at,
        metadata=_revoked_publication_metadata(entry, finished_at=finished_at),
        cancel_requested=True,
    )
    return replace(
        candidate,
        metadata=_merged_metadata(
            candidate,
            metadata_update_fn=pending_metadata_update_fn,
        ),
    )


def _flagged_running_entry(entry: QueueEntry) -> QueueEntry:
    """Flag a running row; its worker delivers the cancellation and terminalizes."""
    return replace(entry, cancel_requested=True)


def _cancel_in_one_mutation(
    queue_store: QueueStore,
    queue_id: str,
    *,
    accepts: _AcceptEntryFn,
    pending_metadata_update_fn: _MetadataUpdateFn | None,
) -> QueueEntry | None:
    """Cancel under one queue mutation: pending rows terminalize, running rows are flagged."""

    def update(entry: QueueEntry) -> tuple[QueueEntry | None, QueueEntry | None]:
        if not accepts(entry):
            return None, None
        if entry.status == QueueStatus.PENDING:
            updated = _cancelled_pending_entry(
                entry,
                finished_at=now_utc_iso(),
                pending_metadata_update_fn=pending_metadata_update_fn,
            )
        elif entry.status == QueueStatus.RUNNING:
            updated = _flagged_running_entry(entry)
        else:
            return None, None
        return updated, updated

    return queue_store.mutate_entry_by_id(queue_id, update, missing_result=None)


def _cancel_pending_with_publication(
    queue_store: QueueStore,
    queue_id: str,
    *,
    accepts: _AcceptEntryFn,
    pending_metadata_update_fn: _MetadataUpdateFn | None,
    before_pending_cancel_fn: Callable[[QueueEntry], Any],
) -> QueueEntry | None:
    """Two-phase pending cancel: durable fence, publication callback, terminal save.

    Persist the dequeue fence before invoking the cross-store publication
    callback. Keep both locks through the final queue transition so no
    successor, worker claim, or metadata mutation can interleave. If
    publication or the final save fails, the pending row remains durably
    cancel-requested and a retry can idempotently finish the exact same
    generation. A running row is only flagged; the callback is not invoked.
    """
    with queue_lock(queue_store.root):
        entries = queue_store.load_entries_fn(queue_store.root)
        for index, entry in enumerate(entries):
            if not isinstance(entry, QueueEntry) or entry.queue_id != queue_id:
                continue
            if not accepts(entry):
                return None
            if entry.status == QueueStatus.RUNNING:
                updated = _flagged_running_entry(entry)
                entries[index] = updated
                queue_store.save_entries_fn(queue_store.root, entries)
                return updated
            if entry.status != QueueStatus.PENDING:
                return None

            finished_at = now_utc_iso()
            fenced = replace(
                entry,
                cancel_requested=True,
                metadata=_revoked_publication_metadata(entry, finished_at=finished_at),
            )
            if fenced != entry:
                entries[index] = fenced
                queue_store.save_entries_fn(queue_store.root, entries)

            updated = _cancelled_pending_entry(
                fenced,
                finished_at=finished_at,
                pending_metadata_update_fn=pending_metadata_update_fn,
            )
            before_pending_cancel_fn(updated)
            entries[index] = updated
            queue_store.save_entries_fn(queue_store.root, entries)
            return updated
        return None


def request_cancel(
    root: str | Path,
    queue_id: str,
    *,
    accept_entry_fn: _AcceptEntryFn | None = None,
    expected_entry: QueueEntry | None = None,
    pending_metadata_update_fn: _MetadataUpdateFn | None = None,
    before_pending_cancel_fn: Callable[[QueueEntry], Any] | None = None,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
) -> QueueEntry | None:
    """Cancel one row: a pending row terminalizes, a running row is flagged.

    Returns the updated row, or ``None`` when no accepted pending/running row
    with ``queue_id`` exists.
    """

    def accepts(entry: QueueEntry) -> bool:
        return _cancel_accepted(
            entry,
            accept_entry_fn=accept_entry_fn,
            expected_entry=expected_entry,
        )

    queue_store = QueueStore.for_root(
        root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    )
    # Publication holds this same entry-scoped lock across its ownership check,
    # external side effects, and COMPLETE transition. Therefore cancellation
    # either revokes ownership before any side effect or terminalizes only after
    # publication has fully completed.
    with queue_record_publication_lock(queue_store.root, queue_id):
        if before_pending_cancel_fn is not None:
            return _cancel_pending_with_publication(
                queue_store,
                queue_id,
                accepts=accepts,
                pending_metadata_update_fn=pending_metadata_update_fn,
                before_pending_cancel_fn=before_pending_cancel_fn,
            )
        return _cancel_in_one_mutation(
            queue_store,
            queue_id,
            accepts=accepts,
            pending_metadata_update_fn=pending_metadata_update_fn,
        )


# --- requeue and terminal marks --------------------------------------------------


def requeue_running_entry(
    root: str | Path,
    queue_id: str,
    *,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
    accept_entry_fn: _AcceptEntryFn | None = None,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
    cancel_metadata_update_fn: _MetadataUpdateFn | None = None,
    requeue_metadata_update: Mapping[str, Any] | None = None,
) -> QueueEntry | None:
    def requeue(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        for index, entry in enumerate(entries):
            if (
                entry.queue_id != queue_id
                or entry.status != QueueStatus.RUNNING
                or (accept_entry_fn is not None and not accept_entry_fn(entry))
                or (
                    expected_entry is not None
                    and not queue_entries_same_generation(entry, expected_entry)
                )
                or (expected_task_id is not None and entry.task_id != expected_task_id)
            ):
                continue
            if entry.cancel_requested:
                # A cancel was requested while this entry was running. Requeueing it
                # for resume would clear cancel_requested and let the worker dequeue
                # and resume the very job the user cancelled. Honor the cancellation
                # instead so the stop is terminal. Workers deliver cancellation as a
                # SIGTERM that the run interprets as a worker-shutdown requeue, so this
                # is the chokepoint that keeps "cancel" from turning into "resume".
                # Clear cancel_requested: it has now been honored, so the terminal
                # entry should not keep advertising a pending cancellation.
                candidate = terminal_entry(entry, status=QueueStatus.CANCELLED, error=None)
                updated = replace(
                    candidate,
                    metadata=_merged_metadata(
                        candidate,
                        metadata_update_fn=cancel_metadata_update_fn,
                    ),
                )
                entries[index] = updated
                return updated, True
            updated = replace(
                entry,
                status=QueueStatus.PENDING,
                started_at="",
                cancel_requested=False,
                error="",
                metadata=_merged_metadata(entry, metadata_update=requeue_metadata_update),
            )
            entries[index] = updated
            return updated, True
        return None, False

    return QueueStore.for_root(
        root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    ).mutate_entries(requeue)


def _mark_status(
    root: str | Path,
    queue_id: str,
    *,
    status: QueueStatus,
    error: str = "",
    metadata_update: dict[str, Any] | None = None,
    metadata_update_fn: _MetadataUpdateFn | None = None,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
    accept_entry_fn: _AcceptEntryFn | None = None,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
    before_update_fn: Callable[[], Any] | None = None,
    require_cancel_requested: bool = False,
) -> QueueEntry | None:
    def update(entry: QueueEntry) -> tuple[QueueEntry | None, QueueEntry | None]:
        if accept_entry_fn is not None and not accept_entry_fn(entry):
            return None, None
        if expected_entry is not None and not queue_entries_same_generation(
            entry,
            expected_entry,
        ):
            return None, None
        if expected_task_id is not None and entry.task_id != expected_task_id:
            return None, None
        if status != QueueStatus.CANCELLED and entry.cancel_requested:
            # Cancellation and terminal completion race under the same queue
            # mutation lock. Once cancellation is acknowledged, a completed or
            # failed writer must not overwrite it; its owner can retry and take
            # the cancelled branch against the durable flag.
            return None, None
        if entry.status in _TERMINAL_STATUSES:
            # Idempotent replays of the authoritative terminal status must
            # still repair artifacts/indexes under the same generation lock.
            # A conflicting terminal writer is rejected and can reconcile to
            # the durable status through its explicit fallback path.
            if entry.status != status:
                return None, None
            merged = _merged_metadata(
                entry,
                metadata_update=metadata_update,
                metadata_update_fn=metadata_update_fn,
            )
            if before_update_fn is not None:
                before_update_fn()
            updated = terminal_entry(
                entry,
                status=status,
                error=error.strip() or None,
                finished_at=entry.finished_at,
                metadata=merged,
            )
            return updated, (updated if updated != entry else None)
        if (
            status == QueueStatus.CANCELLED
            and require_cancel_requested
            and not entry.cancel_requested
        ):
            return None, None
        merged = _merged_metadata(
            entry,
            metadata_update=metadata_update,
            metadata_update_fn=metadata_update_fn,
        )
        if before_update_fn is not None:
            before_update_fn()
        updated = terminal_entry(entry, status=status, error=error, metadata=merged)
        return updated, updated

    return QueueStore.for_root(
        root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    ).mutate_entry_by_id(queue_id, update, missing_result=None)


def correct_terminal_status(
    root: str | Path,
    queue_id: str,
    *,
    status: QueueStatus,
    error: str | None = None,
    metadata_update: Mapping[str, Any] | None = None,
    metadata_update_fn: _MetadataUpdateFn | None = None,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
    accept_entry_fn: _AcceptEntryFn | None = None,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
) -> QueueEntry | None:
    """Correct an already-terminal row to another terminal status.

    This is the recovery-only terminal -> terminal transition: a durable
    state file proved a different outcome than the row records. Active rows
    are refused; they must go through ``mark_*``/``request_cancel`` so their
    side-effect evidence is written in the same queue mutation. The row is
    built by :func:`terminal_entry` (``finished_at`` is re-stamped; ``error``
    ``None`` keeps the recorded error; ``cancel_requested`` is left as
    recorded) and ``metadata_update``/``metadata_update_fn`` are merged under
    the same queue lock.
    """
    target = QueueStatus(status)
    if target not in _TERMINAL_STATUSES:
        raise ValueError(
            f"correct_terminal_status requires a terminal status, got {target.value!r}"
        )

    def update(entry: QueueEntry) -> tuple[QueueEntry | None, QueueEntry | None]:
        if entry.status not in _TERMINAL_STATUSES:
            return None, None
        if accept_entry_fn is not None and not accept_entry_fn(entry):
            return None, None
        if expected_entry is not None and not queue_entries_same_generation(
            entry,
            expected_entry,
        ):
            return None, None
        if expected_task_id is not None and entry.task_id != expected_task_id:
            return None, None
        merged = _merged_metadata(
            entry,
            metadata_update=metadata_update,
            metadata_update_fn=metadata_update_fn,
        )
        updated = terminal_entry(
            entry,
            status=target,
            error=error,
            metadata=merged,
            cancel_requested=entry.cancel_requested,
        )
        return updated, updated

    return QueueStore.for_root(
        root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    ).mutate_entry_by_id(queue_id, update, missing_result=None)


def mark_completed(
    root: str | Path,
    queue_id: str,
    *,
    metadata_update: dict[str, Any] | None = None,
    metadata_update_fn: _MetadataUpdateFn | None = None,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
    accept_entry_fn: _AcceptEntryFn | None = None,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
    before_update_fn: Callable[[], Any] | None = None,
) -> QueueEntry | None:
    return _mark_status(
        root,
        queue_id,
        status=QueueStatus.COMPLETED,
        metadata_update=metadata_update,
        metadata_update_fn=metadata_update_fn,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
        accept_entry_fn=accept_entry_fn,
        expected_entry=expected_entry,
        expected_task_id=expected_task_id,
        before_update_fn=before_update_fn,
    )


def mark_failed(
    root: str | Path,
    queue_id: str,
    *,
    error: str,
    metadata_update: dict[str, Any] | None = None,
    metadata_update_fn: _MetadataUpdateFn | None = None,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
    accept_entry_fn: _AcceptEntryFn | None = None,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
    before_update_fn: Callable[[], Any] | None = None,
) -> QueueEntry | None:
    return _mark_status(
        root,
        queue_id,
        status=QueueStatus.FAILED,
        error=error,
        metadata_update=metadata_update,
        metadata_update_fn=metadata_update_fn,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
        accept_entry_fn=accept_entry_fn,
        expected_entry=expected_entry,
        expected_task_id=expected_task_id,
        before_update_fn=before_update_fn,
    )


def mark_cancelled(
    root: str | Path,
    queue_id: str,
    *,
    error: str = "",
    metadata_update: dict[str, Any] | None = None,
    metadata_update_fn: _MetadataUpdateFn | None = None,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
    accept_entry_fn: _AcceptEntryFn | None = None,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
    before_update_fn: Callable[[], Any] | None = None,
    require_cancel_requested: bool = False,
) -> QueueEntry | None:
    return _mark_status(
        root,
        queue_id,
        status=QueueStatus.CANCELLED,
        error=error,
        metadata_update=metadata_update,
        metadata_update_fn=metadata_update_fn,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
        accept_entry_fn=accept_entry_fn,
        expected_entry=expected_entry,
        expected_task_id=expected_task_id,
        before_update_fn=before_update_fn,
        require_cancel_requested=require_cancel_requested,
    )
