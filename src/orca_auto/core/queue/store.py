"""Durable queue file: storage, locking, duplicate policy, enqueue/dequeue/clear.

``queue.json`` under one queue root holds every row; every read and write
goes through :func:`queue_lock`. :class:`QueueStore` binds a root to its
load/save functions and is the single lock/load/mutate/save primitive that
the module-level operations and :mod:`.transitions` (cancellation and
terminal marks) build on.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Self, TypeVar

from ..utils.lock import FileLockTimeoutError, file_lock
from ..utils.persistence import (
    now_utc_iso,
    resolve_root_path,
    timestamped_token,
)
from . import persistence as _queue_persistence
from .deferral import ADMISSION_DEFERRAL_METADATA_KEY, queue_entry_admission_is_deferred
from .generation import queue_entries_same_generation
from .priority import normalize_queue_priority
from .publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    queue_entry_is_claimable,
    queue_record_publication_lock_path,
    queue_record_sync_metadata,
)
from .types import ACTIVE_QUEUE_STATUSES, TERMINAL_QUEUE_STATUSES, QueueEntry, QueueStatus

QUEUE_FILE_NAME = _queue_persistence.QUEUE_FILE_NAME
QUEUE_LOCK_NAME = _queue_persistence.QUEUE_LOCK_NAME
_ACTIVE_STATUSES = ACTIVE_QUEUE_STATUSES
_TERMINAL_STATUSES = TERMINAL_QUEUE_STATUSES
_QueueEntryT = TypeVar("_QueueEntryT", bound=QueueEntry)
_MutationResultT = TypeVar("_MutationResultT")
_TOKEN_COLLISION_RETRY_LIMIT = 32

QueueDuplicatePolicy = Callable[[Sequence[QueueEntry], QueueEntry], None]
DuplicateErrorFactory = Callable[[str, QueueEntry], Exception]


class DuplicateQueueEntryError(RuntimeError):
    """Raised when an equivalent active task is already queued or running."""


# The persistence layer raises this class directly; the store re-exports it so
# callers catch one class whether they import from here or from the package.
QueueStoreCorruptError = _queue_persistence.QueueStoreCorruptError


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


def _queue_path(root: Path) -> Path:
    return _queue_persistence.queue_path(root)


def _lock_path(root: Path) -> Path:
    return _queue_persistence.queue_lock_path(root)


def entry_to_dict(entry: QueueEntry) -> dict[str, Any]:
    return _queue_persistence.entry_to_dict(entry)


def entry_from_dict(raw: dict[str, Any]) -> QueueEntry:
    return _queue_persistence.entry_from_dict(raw)


def _status_value(status: QueueStatus | str) -> str:
    if isinstance(status, QueueStatus):
        return status.value
    return str(status).strip().lower()


def _status_values(statuses: Collection[QueueStatus | str]) -> set[str]:
    return {_status_value(status) for status in statuses}


def find_entry_by_key(
    entries: Sequence[_QueueEntryT],
    key: str,
    *,
    key_fn: Callable[[_QueueEntryT], str],
    statuses: Collection[QueueStatus | str],
    reverse: bool = False,
) -> _QueueEntryT | None:
    """Find the first entry matching a duplicate key and status set."""
    status_values = _status_values(statuses)
    candidates = reversed(entries) if reverse else entries
    for entry in candidates:
        if key_fn(entry) == key and _status_value(entry.status) in status_values:
            return entry
    return None


def _default_duplicate_key_error(key: str, existing: QueueEntry) -> DuplicateQueueEntryError:
    status = _status_value(existing.status) or "?"
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


def reject_active_task_duplicate(
    entries: Sequence[QueueEntry],
    entry: QueueEntry,
) -> None:
    for existing in entries:
        if existing.app_name != entry.app_name or existing.task_id != entry.task_id:
            continue
        if existing.status in _ACTIVE_STATUSES:
            raise DuplicateQueueEntryError(
                f"Active queue entry already exists for app={entry.app_name} task_id={entry.task_id}"
            )


@contextmanager
def queue_lock(root: str | Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    resolved_root = resolve_root_path(root)
    lock_path = _lock_path(resolved_root)
    with ExitStack() as stack:
        try:
            stack.enter_context(file_lock(lock_path, timeout_seconds=timeout_seconds))
        except FileLockTimeoutError as exc:
            raise QueueLockTimeoutError(str(exc)) from exc
        yield


def load_entries(
    root: str | Path,
    *,
    entry_from_dict_fn: Callable[[dict[str, Any]], QueueEntry] = entry_from_dict,
    corrupt_error: type[Exception] = QueueStoreCorruptError,
) -> list[QueueEntry]:
    return _queue_persistence.load_entries(
        root,
        entry_from_dict_fn=entry_from_dict_fn,
        corrupt_error=corrupt_error,
    )


def save_entries(root: str | Path, entries: Sequence[QueueEntry]) -> None:
    _queue_persistence.save_entries(root, entries, entry_to_dict_fn=entry_to_dict)


@dataclass(frozen=True)
class QueueStore:
    """Persistence facade for one queue root.

    The module-level functions remain the public API. Newer code can use this
    object to keep the queue root plus load/save overrides together instead of
    repeating the lock/load/mutate/save pattern at every call site.
    """

    root: Path
    load_entries_fn: Callable[[Path], list[Any]]
    save_entries_fn: Callable[[Path, Sequence[Any]], Any]

    @classmethod
    def for_root(
        cls,
        root: str | Path,
        *,
        load_entries_fn: Callable[[Path], list[Any]] | None = None,
        save_entries_fn: Callable[[Path, Sequence[Any]], Any] | None = None,
    ) -> Self:
        return cls(
            root=resolve_root_path(root),
            load_entries_fn=load_entries_fn or load_entries,
            save_entries_fn=save_entries_fn or save_entries,
        )

    @property
    def path(self) -> Path:
        return _queue_path(self.root)

    def list_entries(self, *, timeout_seconds: float = 10.0) -> list[Any]:
        with queue_lock(self.root, timeout_seconds=timeout_seconds):
            return self.load_entries_fn(self.root)

    def mutate_entries(
        self,
        mutator: Callable[[list[Any]], tuple[_MutationResultT, bool]],
        *,
        after_commit_fn: Callable[[], Any] | None = None,
    ) -> _MutationResultT:
        with queue_lock(self.root):
            entries = self.load_entries_fn(self.root)
            original_entries = list(entries)
            result, changed = mutator(entries)
            if changed:
                self.save_entries_fn(self.root, entries)
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
                            self.save_entries_fn(self.root, original_entries)
                        except BaseException as rollback_error:  # noqa: BLE001
                            compensation_error = rollback_error
                            try:
                                visible_entries = list(self.load_entries_fn(self.root))
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
        self,
        queue_id: str,
        updater: Callable[[QueueEntry], tuple[_MutationResultT, QueueEntry | None]],
        *,
        missing_result: _MutationResultT,
    ) -> _MutationResultT:
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

        return self.mutate_entries(mutate)


def list_queue(
    root: str | Path,
    *,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
) -> list[QueueEntry]:
    return QueueStore.for_root(root, load_entries_fn=load_entries_fn).list_entries()


def mutate_entries(
    root: str | Path,
    mutator: Callable[[list[Any]], tuple[_MutationResultT, bool]],
    *,
    load_entries_fn: Callable[[Path], list[Any]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[Any]], Any] | None = None,
    after_commit_fn: Callable[[], Any] | None = None,
) -> _MutationResultT:
    return QueueStore.for_root(
        root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    ).mutate_entries(mutator, after_commit_fn=after_commit_fn)


def mutate_entry_by_id(
    root: str | Path,
    queue_id: str,
    updater: Callable[[QueueEntry], tuple[_MutationResultT, QueueEntry | None]],
    *,
    missing_result: _MutationResultT,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
) -> _MutationResultT:
    return QueueStore.for_root(
        root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    ).mutate_entry_by_id(queue_id, updater, missing_result=missing_result)


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
    if not _queue_path(resolved_root).exists():
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

    removed_count = QueueStore.for_root(
        resolved_root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    ).mutate_entries(clear)
    # A removed row's publication lock file has no owner left; a row that
    # never went through the publication lock has no file to remove.
    for queue_id in removed_ids:
        queue_record_publication_lock_path(resolved_root, queue_id).unlink(missing_ok=True)
    return removed_count


def enqueue(
    root: str | Path,
    *,
    app_name: str,
    task_id: str,
    task_kind: str,
    engine: str,
    priority: int = 10,
    metadata: dict[str, Any] | None = None,
    duplicate_policy: QueueDuplicatePolicy | None = None,
    before_commit_fn: Callable[[], Any] | None = None,
    after_commit_fn: Callable[[], Any] | None = None,
) -> QueueEntry:
    resolved_root = resolve_root_path(root)
    reject_duplicate = duplicate_policy or reject_active_task_duplicate
    normalized_priority = normalize_queue_priority(priority)

    def append(entries: list[QueueEntry]) -> tuple[QueueEntry, bool]:
        occupied_queue_ids = {entry.queue_id for entry in entries}
        queue_id = ""
        for _attempt in range(_TOKEN_COLLISION_RETRY_LIMIT):
            candidate = timestamped_token("q")
            if candidate not in occupied_queue_ids:
                queue_id = candidate
                break
        if not queue_id:
            raise RuntimeError(
                "Could not allocate a unique queue id after "
                f"{_TOKEN_COLLISION_RETRY_LIMIT} attempts"
            )
        entry_metadata = dict(metadata or {})
        if QUEUE_RECORD_SYNC_KEY not in entry_metadata:
            entry_metadata.update(
                queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_COMPLETE,
                    token=queue_id,
                    owner_pid=0,
                )
            )
        entry = QueueEntry(
            queue_id=queue_id,
            app_name=app_name.strip(),
            task_id=task_id.strip(),
            task_kind=task_kind.strip(),
            engine=engine.strip(),
            priority=normalized_priority,
            enqueued_at=now_utc_iso(),
            metadata=entry_metadata,
        )
        reject_duplicate(entries, entry)
        if before_commit_fn is not None:
            before_commit_fn()
        entries.append(entry)
        return entry, True

    return QueueStore.for_root(resolved_root).mutate_entries(
        append,
        after_commit_fn=after_commit_fn,
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
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
    expected_entry: QueueEntry | None = None,
) -> QueueEntry | None:
    """Mark one selected pending entry running if it is still eligible."""

    def dequeue(entry: QueueEntry) -> tuple[QueueEntry | None, QueueEntry | None]:
        if (
            (accept_entry_fn is not None and not accept_entry_fn(entry))
            or (expected_entry is not None and entry != expected_entry)
            or entry.status != QueueStatus.PENDING
            or entry.cancel_requested
            or not queue_entry_is_claimable(entry)
            or queue_entry_admission_is_deferred(entry)
        ):
            return None, None
        updated = _claimed(entry)
        return updated, updated

    return QueueStore.for_root(
        root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    ).mutate_entry_by_id(queue_id, dequeue, missing_result=None)


class QueueCancellationProbe:
    """Memoize one generation's cancellation until the canonical file changes.

    Keep only a boolean and file identity, never a second authoritative store or
    a retained copy of queue history. Writers replace queue.json under its lock.
    A concurrent commit after the fast stat is observed on the next poll.
    """

    def __init__(
        self,
        store: QueueStore,
        queue_id: str,
        *,
        accept_entry_fn: Callable[[QueueEntry], bool],
    ) -> None:
        self.store = store
        self.queue_id = queue_id
        self.accept_entry_fn = accept_entry_fn
        self._signature: tuple[int, ...] | None = None
        self._initialized = False
        self._cancel_requested = False

    def _file_signature(self) -> tuple[int, ...] | None:
        try:
            stat = self.store.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def __call__(self) -> bool:
        signature = self._file_signature()
        if self._initialized and signature == self._signature:
            return self._cancel_requested
        with queue_lock(self.store.root, timeout_seconds=0.0):
            entries = self.store.load_entries_fn(self.store.root)
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
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
    lock_timeout_seconds: float = 10.0,
) -> bool:
    entries = QueueStore.for_root(root, load_entries_fn=load_entries_fn).list_entries(
        timeout_seconds=lock_timeout_seconds
    )
    for entry in entries:
        if (
            entry.queue_id == queue_id
            and (accept_entry_fn is None or accept_entry_fn(entry))
            and (expected_entry is None or queue_entries_same_generation(entry, expected_entry))
            and (expected_task_id is None or entry.task_id == expected_task_id)
        ):
            return bool(entry.cancel_requested)
    return False


def update_metadata(
    root: str | Path,
    queue_id: str,
    metadata_update: dict[str, Any],
    *,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
) -> QueueEntry | None:
    """Merge metadata without changing lifecycle state or a fenced generation."""

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
        merged = dict(entry.metadata)
        merged.update(metadata_update)
        if merged == entry.metadata:
            return entry, None
        updated = replace(entry, metadata=merged)
        return updated, updated

    return QueueStore.for_root(
        root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    ).mutate_entry_by_id(queue_id, update, missing_result=None)
