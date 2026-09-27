"""Durable queue file: storage, locking, duplicate-key policy, dequeue/clear.

``queue.json`` under one queue root holds every row; every read and write
goes through :func:`queue_lock`. :func:`mutate_entries` is the single
lock/load/mutate/save primitive, with the after-commit compensation, and
:func:`mutate_entry_by_id` its one-row form; the module-level operations and
:mod:`.transitions` (cancellation and terminal marks) build on them. Two
writers still bypass them: ``orca.queue.orphans`` rewrites rows under a raw
``queue_lock``/``load_entries``/``save_entries`` sequence, and
:func:`clear_terminal` takes a load/save override so its caller can remove
worker logs inside the lock.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, TypeVar

from ..utils.lock import FileLockTimeoutError, file_lock
from ..utils.persistence import now_utc_iso, resolve_root_path
from .deferral import ADMISSION_DEFERRAL_METADATA_KEY, queue_entry_admission_is_deferred
from .persistence import (
    QUEUE_LOCK_NAME,
    QueueStoreCorruptError,
    entry_from_dict,
    entry_to_dict,
    load_entries,
    queue_lock_path,
    queue_path,
    save_entries,
)
from .publication import queue_entry_is_claimable, queue_record_publication_lock_path
from .types import ACTIVE_QUEUE_STATUSES, TERMINAL_QUEUE_STATUSES, QueueEntry, QueueStatus

_ACTIVE_STATUSES = ACTIVE_QUEUE_STATUSES
_TERMINAL_STATUSES = TERMINAL_QUEUE_STATUSES
_QueueEntryT = TypeVar("_QueueEntryT", bound=QueueEntry)
_MutationResultT = TypeVar("_MutationResultT")

DuplicateErrorFactory = Callable[[str, QueueEntry], Exception]


class DuplicateQueueEntryError(RuntimeError):
    """Raised when an equivalent active task is already queued or running."""


class QueueLockTimeoutError(TimeoutError):
    """Raised only when acquiring the queue lock reaches its deadline."""


QueueCompensationOutcome = Literal["restored", "not_restored", "unknown"]


class QueueAfterCommitError(RuntimeError):
    """Preserve an after-commit rejection and its compensation outcome."""

    def __init__(
        self,
        *,
        after_commit_error: BaseException,
        compensation_outcome: QueueCompensationOutcome,
        provisional_result: Any,
        compensation_error: BaseException | None = None,
        verification_error: BaseException | None = None,
    ) -> None:
        self.after_commit_error = after_commit_error
        self.compensation_outcome = compensation_outcome
        self.provisional_result = provisional_result
        self.compensation_error = compensation_error
        self.verification_error = verification_error
        details = [
            "after-commit contract failed "
            f"({after_commit_error.__class__.__name__}: {after_commit_error})",
            f"queue compensation outcome={compensation_outcome}",
        ]
        if compensation_error is not None:
            details.append(
                f"compensation error={compensation_error.__class__.__name__}: {compensation_error}"
            )
        if verification_error is not None:
            details.append(
                f"verification error={verification_error.__class__.__name__}: {verification_error}"
            )
        super().__init__("; ".join(details))

    @property
    def compensation_succeeded(self) -> bool:
        return self.compensation_outcome == "restored"


def find_entry_by_key(
    entries: Sequence[_QueueEntryT],
    key: str,
    *,
    key_fn: Callable[[_QueueEntryT], str],
    statuses: Collection[QueueStatus | str],
    reverse: bool = False,
) -> _QueueEntryT | None:
    """Find the first entry matching a duplicate key and status set."""
    candidates = reversed(entries) if reverse else entries
    for entry in candidates:
        if key_fn(entry) == key and entry.status in statuses:
            return entry
    return None


def _default_duplicate_key_error(key: str, existing: QueueEntry) -> DuplicateQueueEntryError:
    status = existing.status.value
    qid = existing.queue_id or "?"
    return DuplicateQueueEntryError(
        f"Queue entry already exists for key={key} (queue_id={qid}, status={status})"
    )


def reject_duplicate_entry_key(
    entries: Sequence[QueueEntry],
    *,
    key: str,
    key_fn: Callable[[QueueEntry], str],
    force: bool = False,
    active_statuses: Collection[QueueStatus | str] = _ACTIVE_STATUSES,
    terminal_statuses: Collection[QueueStatus | str] = _TERMINAL_STATUSES,
    error_factory: DuplicateErrorFactory | None = None,
) -> None:
    """Reject duplicate active entries and, unless forced, terminal entries.

    This helper lets adapters define duplicate identity by any stable key while
    sharing the active/terminal/force policy used by queue-backed workloads.
    """
    make_error = error_factory or _default_duplicate_key_error
    active = find_entry_by_key(
        entries,
        key,
        key_fn=key_fn,
        statuses=active_statuses,
    )
    if active is not None:
        raise make_error(key, active)

    if force:
        return

    terminal = find_entry_by_key(
        entries,
        key,
        key_fn=key_fn,
        statuses=terminal_statuses,
        reverse=True,
    )
    if terminal is not None:
        raise make_error(key, terminal)


@contextmanager
def queue_lock(root: str | Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    resolved_root = resolve_root_path(root)
    lock_path = queue_lock_path(resolved_root)
    with ExitStack() as stack:
        try:
            stack.enter_context(file_lock(lock_path, timeout_seconds=timeout_seconds))
        except FileLockTimeoutError as exc:
            raise QueueLockTimeoutError(str(exc)) from exc
        yield


def list_queue(root: str | Path, *, lock_timeout_seconds: float = 10.0) -> list[QueueEntry]:
    resolved_root = resolve_root_path(root)
    with queue_lock(resolved_root, timeout_seconds=lock_timeout_seconds):
        return load_entries(resolved_root)


def mutate_entries(
    root: str | Path,
    mutator: Callable[[list[Any]], tuple[_MutationResultT, bool]],
    *,
    after_commit_fn: Callable[[], Any] | None = None,
    load_entries_fn: Callable[[Path], list[Any]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[Any]], Any] | None = None,
) -> _MutationResultT:
    """Load, mutate and save the rows under the queue lock.

    ``mutator`` returns its result and whether it changed the rows; unchanged
    rows are not saved. When ``after_commit_fn`` rejects the saved rows, the
    original rows are written back under the same lock and the rejection is
    raised as :class:`QueueAfterCommitError` with the compensation outcome.
    Only :func:`clear_terminal` passes a load/save override.
    """
    resolved_root = resolve_root_path(root)
    load = load_entries_fn or load_entries
    save = save_entries_fn or save_entries
    with queue_lock(resolved_root):
        entries = load(resolved_root)
        original_entries = list(entries)
        result, changed = mutator(entries)
        if changed:
            save(resolved_root, entries)
            if after_commit_fn is not None:
                try:
                    after_commit_fn()
                except BaseException as after_commit_error:
                    # The queue lock is still held, so no worker can claim
                    # the provisional row before its failed publication
                    # contract is compensated.
                    compensation_error: BaseException | None = None
                    verification_error: BaseException | None = None
                    compensation_outcome: QueueCompensationOutcome = "restored"
                    try:
                        save(resolved_root, original_entries)
                    except BaseException as rollback_error:  # noqa: BLE001
                        compensation_error = rollback_error
                        try:
                            visible_entries = list(load(resolved_root))
                            if visible_entries == entries:
                                compensation_outcome = "not_restored"
                            else:
                                # A compensation write that reported an
                                # error is not durably proven even when a
                                # reload currently resembles the original
                                # rows. Only a clean save return establishes
                                # the restored outcome.
                                compensation_outcome = "unknown"
                        except BaseException as reload_error:  # noqa: BLE001
                            compensation_outcome = "unknown"
                            verification_error = reload_error
                    raise QueueAfterCommitError(
                        after_commit_error=after_commit_error,
                        compensation_outcome=compensation_outcome,
                        provisional_result=result,
                        compensation_error=compensation_error,
                        verification_error=verification_error,
                    ) from after_commit_error
        return result


def mutate_entry_by_id(
    root: str | Path,
    queue_id: str,
    updater: Callable[[QueueEntry], tuple[_MutationResultT, QueueEntry | None]],
    *,
    missing_result: _MutationResultT,
) -> _MutationResultT:
    """Apply ``updater`` to the row ``queue_id``; an updater returning no row saves nothing."""

    def mutate(entries: list[Any]) -> tuple[_MutationResultT, bool]:
        for index, entry in enumerate(entries):
            if not isinstance(entry, QueueEntry) or entry.queue_id != queue_id:
                continue
            result, updated_entry = updater(entry)
            if updated_entry is None:
                return result, False
            entries[index] = updated_entry
            return result, True
        return missing_result, False

    return mutate_entries(root, mutate)


def _entry_timestamp(entry: QueueEntry) -> str:
    return entry.finished_at or entry.started_at or entry.enqueued_at


def clear_terminal(
    root: str | Path,
    *,
    keep_last: int = 0,
    retain_entry_fn: Callable[[QueueEntry], bool] | None = None,
    select_entry_fn: Callable[[QueueEntry], bool] | None = None,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
) -> int:
    resolved_root = resolve_root_path(root)
    if not queue_path(resolved_root).exists():
        return 0
    removed_ids: list[str] = []

    def clear(entries: list[QueueEntry]) -> tuple[int, bool]:
        terminal_entries = [
            entry
            for entry in entries
            if entry.status in _TERMINAL_STATUSES
            and (select_entry_fn is None or select_entry_fn(entry))
        ]
        if not terminal_entries:
            return 0, False

        kept_terminal_ids: set[str] = set()
        if retain_entry_fn is not None:
            kept_terminal_ids.update(
                entry.queue_id for entry in terminal_entries if retain_entry_fn(entry)
            )
        if keep_last > 0:
            terminal_entries = sorted(
                terminal_entries,
                key=lambda entry: (_entry_timestamp(entry), entry.queue_id),
                reverse=True,
            )
            kept_terminal_ids.update(entry.queue_id for entry in terminal_entries[:keep_last])

        kept_entries = [
            entry
            for entry in entries
            if entry.status not in _TERMINAL_STATUSES
            or (select_entry_fn is not None and not select_entry_fn(entry))
            or entry.queue_id in kept_terminal_ids
        ]
        removed_count = len(entries) - len(kept_entries)
        if removed_count <= 0:
            return 0, False
        kept_ids = {entry.queue_id for entry in kept_entries}
        removed_ids.extend(entry.queue_id for entry in entries if entry.queue_id not in kept_ids)
        entries[:] = kept_entries
        return removed_count, True

    removed_count = mutate_entries(
        resolved_root,
        clear,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    )
    # A removed row's publication lock file has no owner left; a row that
    # never went through the publication lock has no file to remove.
    for queue_id in removed_ids:
        queue_record_publication_lock_path(resolved_root, queue_id).unlink(missing_ok=True)
    return removed_count


def claimable_pending(entry: QueueEntry) -> bool:
    """A pending, uncancelled, published row whose admission is not deferred."""
    return (
        entry.status == QueueStatus.PENDING
        and not entry.cancel_requested
        and queue_entry_is_claimable(entry)
        and not queue_entry_admission_is_deferred(entry)
    )


def _claimed(entry: QueueEntry) -> QueueEntry:
    # A claim answers the deferral. Dropping it here keeps it from describing
    # a row that later returns to pending for another reason.
    metadata = {
        key: value
        for key, value in entry.metadata.items()
        if key != ADMISSION_DEFERRAL_METADATA_KEY
    }
    return replace(
        entry,
        status=QueueStatus.RUNNING,
        started_at=now_utc_iso(),
        metadata=metadata,
    )


def dequeue_entry_if_pending(
    root: str | Path,
    queue_id: str,
    *,
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
) -> QueueEntry | None:
    """Mark one selected pending entry running if it is still eligible.

    ``accept_entry_fn`` is the caller's generation fence; the claim adds only
    :func:`claimable_pending`.
    """

    def dequeue(entry: QueueEntry) -> tuple[QueueEntry | None, QueueEntry | None]:
        if (accept_entry_fn is not None and not accept_entry_fn(entry)) or not claimable_pending(
            entry
        ):
            return None, None
        updated = _claimed(entry)
        return updated, updated

    return mutate_entry_by_id(root, queue_id, dequeue, missing_result=None)


class QueueCancellationProbe:
    """Memoize one generation's cancellation until the canonical file changes.

    Keep only a boolean and file identity, never a second authoritative store or
    a retained copy of queue history. Writers replace queue.json under its lock.
    A concurrent commit after the fast stat is observed on the next poll.
    """

    def __init__(
        self,
        root: str | Path,
        queue_id: str,
        *,
        accept_entry_fn: Callable[[QueueEntry], bool],
    ) -> None:
        self.root = resolve_root_path(root)
        self.queue_id = queue_id
        self.accept_entry_fn = accept_entry_fn
        self._signature: tuple[int, ...] | None = None
        self._initialized = False
        self._cancel_requested = False

    def _file_signature(self) -> tuple[int, ...] | None:
        try:
            stat = queue_path(self.root).stat()
        except FileNotFoundError:
            return None
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def __call__(self) -> bool:
        signature = self._file_signature()
        if self._initialized and signature == self._signature:
            return self._cancel_requested
        with queue_lock(self.root, timeout_seconds=0.0):
            entries = load_entries(self.root)
            result = any(
                entry.queue_id == self.queue_id
                and self.accept_entry_fn(entry)
                and entry.cancel_requested
                for entry in entries
            )
            # Bind the value and signature under the SAME lock. A post-unlock
            # stat could label an old False with a newly committed cancellation.
            signature = self._file_signature()
            self._signature = signature
            self._cancel_requested = result
            self._initialized = True
        return result


def get_cancel_requested(
    root: str | Path,
    queue_id: str,
    *,
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
    lock_timeout_seconds: float = 10.0,
) -> bool:
    for entry in list_queue(root, lock_timeout_seconds=lock_timeout_seconds):
        if entry.queue_id == queue_id and (accept_entry_fn is None or accept_entry_fn(entry)):
            return bool(entry.cancel_requested)
    return False


def update_metadata(
    root: str | Path,
    queue_id: str,
    metadata_update: dict[str, Any],
    *,
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
) -> QueueEntry | None:
    """Merge metadata without changing lifecycle state or a fenced generation."""

    def update(entry: QueueEntry) -> tuple[QueueEntry | None, QueueEntry | None]:
        if accept_entry_fn is not None and not accept_entry_fn(entry):
            return None, None
        merged = dict(entry.metadata)
        merged.update(metadata_update)
        if merged == entry.metadata:
            return entry, None
        updated = replace(entry, metadata=merged)
        return updated, updated

    return mutate_entry_by_id(root, queue_id, update, missing_result=None)


__all__ = [
    "QUEUE_LOCK_NAME",
    "DuplicateErrorFactory",
    "DuplicateQueueEntryError",
    "QueueAfterCommitError",
    "QueueCancellationProbe",
    "QueueCompensationOutcome",
    "QueueEntry",
    "QueueLockTimeoutError",
    "QueueStoreCorruptError",
    "claimable_pending",
    "clear_terminal",
    "dequeue_entry_if_pending",
    "entry_from_dict",
    "entry_to_dict",
    "find_entry_by_key",
    "get_cancel_requested",
    "list_queue",
    "load_entries",
    "mutate_entries",
    "mutate_entry_by_id",
    "queue_lock",
    "reject_duplicate_entry_key",
    "save_entries",
    "update_metadata",
]
