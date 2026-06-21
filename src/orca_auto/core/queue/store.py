from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator, TypeVar

from ..utils.lock import file_lock
from ..utils.persistence import (
    now_utc_iso,
    resolve_root_path,
    timestamped_token,
)
from . import persistence as _queue_persistence
from .types import QueueEntry, QueueStatus

QUEUE_FILE_NAME = _queue_persistence.QUEUE_FILE_NAME
QUEUE_LOCK_NAME = _queue_persistence.QUEUE_LOCK_NAME
_ACTIVE_STATUSES = frozenset({QueueStatus.PENDING, QueueStatus.RUNNING})
_TERMINAL_STATUSES = frozenset({QueueStatus.COMPLETED, QueueStatus.FAILED, QueueStatus.CANCELLED})
_QueueEntryT = TypeVar("_QueueEntryT", bound=QueueEntry)
_MutationResultT = TypeVar("_MutationResultT")

QueueDuplicatePolicy = Callable[[Sequence[QueueEntry], QueueEntry], None]
DuplicateErrorFactory = Callable[[str, QueueEntry], Exception]


class DuplicateQueueEntryError(RuntimeError):
    """Raised when an equivalent active task is already queued or running."""


class QueueStoreCorruptError(_queue_persistence.QueueStoreCorruptError):
    """Raised when the queue file exists but cannot be safely loaded."""


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
    with file_lock(_lock_path(resolved_root), timeout_seconds=timeout_seconds):
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
    ) -> QueueStore:
        return cls(
            root=resolve_root_path(root),
            load_entries_fn=load_entries_fn or load_entries,
            save_entries_fn=save_entries_fn or save_entries,
        )

    @property
    def path(self) -> Path:
        return _queue_path(self.root)

    def list_entries(self) -> list[Any]:
        with queue_lock(self.root):
            return self.load_entries_fn(self.root)

    def mutate_entries(self, mutator: Callable[[list[Any]], tuple[Any, bool]]) -> Any:
        with queue_lock(self.root):
            entries = self.load_entries_fn(self.root)
            result, changed = mutator(entries)
            if changed:
                self.save_entries_fn(self.root, entries)
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
    mutator: Callable[[list[Any]], tuple[Any, bool]],
    *,
    load_entries_fn: Callable[[Path], list[Any]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[Any]], Any] | None = None,
) -> Any:
    return QueueStore.for_root(
        root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    ).mutate_entries(mutator)


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
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
) -> int:
    resolved_root = resolve_root_path(root)
    if not _queue_path(resolved_root).exists():
        return 0

    def clear(entries: list[QueueEntry]) -> tuple[int, bool]:
        terminal_entries = [entry for entry in entries if entry.status in _TERMINAL_STATUSES]
        if not terminal_entries:
            return 0, False

        kept_terminal_ids: set[str] = set()
        if keep_last > 0:
            terminal_entries = sorted(
                terminal_entries,
                key=lambda entry: (_entry_timestamp(entry), entry.queue_id),
                reverse=True,
            )
            kept_terminal_ids = {entry.queue_id for entry in terminal_entries[:keep_last]}

        kept_entries = [
            entry
            for entry in entries
            if entry.status not in _TERMINAL_STATUSES or entry.queue_id in kept_terminal_ids
        ]
        removed_count = len(entries) - len(kept_entries)
        if removed_count <= 0:
            return 0, False
        entries[:] = kept_entries
        return removed_count, True

    return QueueStore.for_root(
        resolved_root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    ).mutate_entries(clear)


def enqueue_entry(
    root: str | Path,
    entry: QueueEntry,
    *,
    duplicate_policy: QueueDuplicatePolicy | None = None,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
) -> QueueEntry:
    reject_duplicate = duplicate_policy or reject_active_task_duplicate

    def append(entries: list[QueueEntry]) -> tuple[QueueEntry, bool]:
        reject_duplicate(entries, entry)
        entries.append(entry)
        return entry, True

    return QueueStore.for_root(
        root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    ).mutate_entries(append)


def enqueue(
    root: str | Path,
    *,
    app_name: str,
    task_id: str,
    task_kind: str,
    engine: str,
    priority: int = 10,
    metadata: dict[str, Any] | None = None,
) -> QueueEntry:
    resolved_root = resolve_root_path(root)
    entry = QueueEntry(
        queue_id=timestamped_token("q"),
        app_name=app_name.strip(),
        task_id=task_id.strip(),
        task_kind=task_kind.strip(),
        engine=engine.strip(),
        priority=int(priority),
        enqueued_at=now_utc_iso(),
        metadata=dict(metadata or {}),
    )
    return enqueue_entry(resolved_root, entry)


def dequeue_next(
    root: str | Path,
    *,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
) -> QueueEntry | None:
    def dequeue(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        pending = [
            (entry.priority, entry.enqueued_at, index, entry)
            for index, entry in enumerate(entries)
            if entry.status == QueueStatus.PENDING and not entry.cancel_requested
        ]
        if not pending:
            return None, False
        _, _, index, current = min(pending, key=lambda item: (item[0], item[1], item[2]))
        updated = replace(current, status=QueueStatus.RUNNING, started_at=now_utc_iso())
        entries[index] = updated
        return updated, True

    return QueueStore.for_root(
        root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    ).mutate_entries(dequeue)


def request_cancel(
    root: str | Path,
    queue_id: str,
    *,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
) -> QueueEntry | None:
    def update(entry: QueueEntry) -> tuple[QueueEntry | None, QueueEntry | None]:
        if entry.status == QueueStatus.PENDING:
            updated = replace(
                entry,
                status=QueueStatus.CANCELLED,
                cancel_requested=True,
                finished_at=now_utc_iso(),
            )
        elif entry.status == QueueStatus.RUNNING:
            updated = replace(entry, cancel_requested=True)
        else:
            return None, None
        return updated, updated

    return QueueStore.for_root(
        root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    ).mutate_entry_by_id(queue_id, update, missing_result=None)


def get_cancel_requested(
    root: str | Path,
    queue_id: str,
    *,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
) -> bool:
    entries = QueueStore.for_root(root, load_entries_fn=load_entries_fn).list_entries()
    for entry in entries:
        if entry.queue_id == queue_id:
            return bool(entry.cancel_requested)
    return False


def requeue_running_entry(
    root: str | Path,
    queue_id: str,
    *,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
) -> QueueEntry | None:
    def requeue(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        for index, entry in enumerate(entries):
            if entry.queue_id != queue_id or entry.status != QueueStatus.RUNNING:
                continue
            if entry.cancel_requested:
                # A cancel was requested while this entry was running. Requeueing it
                # for resume would clear cancel_requested and let the worker dequeue
                # and resume the very job the user cancelled. Honor the cancellation
                # instead so the stop is terminal. Workers deliver cancellation as a
                # SIGTERM that the run interprets as a worker-shutdown requeue, so this
                # is the chokepoint that keeps "cancel" from turning into "resume".
                updated = replace(
                    entry,
                    status=QueueStatus.CANCELLED,
                    finished_at=now_utc_iso(),
                )
                entries[index] = updated
                return updated, True
            updated = replace(
                entry,
                status=QueueStatus.PENDING,
                started_at="",
                cancel_requested=False,
                error="",
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
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
) -> QueueEntry | None:
    def update(entry: QueueEntry) -> tuple[QueueEntry | None, QueueEntry | None]:
        merged = dict(entry.metadata)
        if metadata_update:
            merged.update(metadata_update)
        updated = replace(
            entry,
            status=status,
            finished_at=now_utc_iso(),
            error=error.strip(),
            metadata=merged,
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
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
) -> QueueEntry | None:
    return _mark_status(
        root,
        queue_id,
        status=QueueStatus.COMPLETED,
        metadata_update=metadata_update,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    )


def mark_failed(
    root: str | Path,
    queue_id: str,
    *,
    error: str,
    metadata_update: dict[str, Any] | None = None,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
) -> QueueEntry | None:
    return _mark_status(
        root,
        queue_id,
        status=QueueStatus.FAILED,
        error=error,
        metadata_update=metadata_update,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    )


def mark_cancelled(
    root: str | Path,
    queue_id: str,
    *,
    error: str = "",
    metadata_update: dict[str, Any] | None = None,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
) -> QueueEntry | None:
    return _mark_status(
        root,
        queue_id,
        status=QueueStatus.CANCELLED,
        error=error,
        metadata_update=metadata_update,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    )
