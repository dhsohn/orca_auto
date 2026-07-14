from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, TypeVar

from ..utils.lock import file_lock
from ..utils.persistence import (
    now_utc_iso,
    resolve_root_path,
    timestamped_token,
)
from . import persistence as _queue_persistence
from .generation import queue_entries_same_generation
from .priority import normalize_queue_priority
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
    queue_entry_is_claimable,
    queue_record_publication_lock,
)
from .types import QueueEntry, QueueStatus

QUEUE_FILE_NAME = _queue_persistence.QUEUE_FILE_NAME
QUEUE_LOCK_NAME = _queue_persistence.QUEUE_LOCK_NAME
_ACTIVE_STATUSES = frozenset({QueueStatus.PENDING, QueueStatus.RUNNING})
_TERMINAL_STATUSES = frozenset({QueueStatus.COMPLETED, QueueStatus.FAILED, QueueStatus.CANCELLED})
_QueueEntryT = TypeVar("_QueueEntryT", bound=QueueEntry)
_MutationResultT = TypeVar("_MutationResultT")
_TOKEN_COLLISION_RETRY_LIMIT = 32

_MetadataUpdateFn = Callable[[QueueEntry], Mapping[str, Any] | None]

QueueDuplicatePolicy = Callable[[Sequence[QueueEntry], QueueEntry], None]
DuplicateErrorFactory = Callable[[str, QueueEntry], Exception]


class DuplicateQueueEntryError(RuntimeError):
    """Raised when an equivalent active task is already queued or running."""


class QueueStoreCorruptError(_queue_persistence.QueueStoreCorruptError):
    """Raised when the queue file exists but cannot be safely loaded."""


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

    def mutate_entries(
        self,
        mutator: Callable[[list[Any]], tuple[Any, bool]],
        *,
        after_commit_fn: Callable[[], Any] | None = None,
    ) -> Any:
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
    mutator: Callable[[list[Any]], tuple[Any, bool]],
    *,
    load_entries_fn: Callable[[Path], list[Any]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[Any]], Any] | None = None,
    after_commit_fn: Callable[[], Any] | None = None,
) -> Any:
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
    normalized_entry = replace(entry, priority=normalize_queue_priority(entry.priority))

    def append(entries: list[QueueEntry]) -> tuple[QueueEntry, bool]:
        reject_duplicate(entries, normalized_entry)
        entries.append(normalized_entry)
        return normalized_entry, True

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
        entry = QueueEntry(
            queue_id=queue_id,
            app_name=app_name.strip(),
            task_id=task_id.strip(),
            task_kind=task_kind.strip(),
            engine=engine.strip(),
            priority=normalized_priority,
            enqueued_at=now_utc_iso(),
            metadata=dict(metadata or {}),
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


def dequeue_next(
    root: str | Path,
    *,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
) -> QueueEntry | None:
    def dequeue(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        pending = [
            (entry.priority, entry.enqueued_at, index, entry)
            for index, entry in enumerate(entries)
            if entry.status == QueueStatus.PENDING
            and not entry.cancel_requested
            and queue_entry_is_claimable(entry)
            and (accept_entry_fn is None or accept_entry_fn(entry))
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
        ):
            return None, None
        updated = replace(entry, status=QueueStatus.RUNNING, started_at=now_utc_iso())
        return updated, updated

    return QueueStore.for_root(
        root,
        load_entries_fn=load_entries_fn,
        save_entries_fn=save_entries_fn,
    ).mutate_entry_by_id(queue_id, dequeue, missing_result=None)


def request_cancel(
    root: str | Path,
    queue_id: str,
    *,
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
    expected_entry: QueueEntry | None = None,
    pending_metadata_update_fn: _MetadataUpdateFn | None = None,
    before_pending_cancel_fn: Callable[[QueueEntry], Any] | None = None,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
) -> QueueEntry | None:
    def update(entry: QueueEntry) -> tuple[QueueEntry | None, QueueEntry | None]:
        if accept_entry_fn is not None and not accept_entry_fn(entry):
            return None, None
        if expected_entry is not None and not queue_entries_same_generation(
            entry,
            expected_entry,
        ):
            return None, None
        if entry.status == QueueStatus.PENDING:
            finished_at = now_utc_iso()
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
            candidate = replace(
                entry,
                status=QueueStatus.CANCELLED,
                cancel_requested=True,
                finished_at=finished_at,
                metadata=metadata,
            )
            updated = replace(
                candidate,
                metadata=_merged_metadata(
                    candidate,
                    metadata_update_fn=pending_metadata_update_fn,
                ),
            )
            if before_pending_cancel_fn is not None:
                # This runs while both the entry-scoped publication lock and
                # queue mutation lock are held. Engine adapters can therefore
                # publish generation-bound terminal artifacts before the
                # durable queue transition makes a replacement submission
                # eligible. An exception aborts the queue write and leaves the
                # pending entry unchanged for explicit reconciliation.
                before_pending_cancel_fn(updated)
        elif entry.status == QueueStatus.RUNNING:
            updated = replace(entry, cancel_requested=True)
        else:
            return None, None
        return updated, updated

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
        return queue_store.mutate_entry_by_id(queue_id, update, missing_result=None)


def get_cancel_requested(
    root: str | Path,
    queue_id: str,
    *,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
) -> bool:
    entries = QueueStore.for_root(root, load_entries_fn=load_entries_fn).list_entries()
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


def requeue_running_entry(
    root: str | Path,
    queue_id: str,
    *,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
    expected_entry: QueueEntry | None = None,
    expected_task_id: str | None = None,
    cancel_metadata_update_fn: _MetadataUpdateFn | None = None,
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
                candidate = replace(
                    entry,
                    status=QueueStatus.CANCELLED,
                    finished_at=now_utc_iso(),
                    cancel_requested=False,
                )
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
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
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
            updated = replace(
                entry,
                cancel_requested=(
                    False if status == QueueStatus.CANCELLED else entry.cancel_requested
                ),
                error=error.strip() or entry.error,
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
        updated = replace(
            entry,
            status=status,
            finished_at=now_utc_iso(),
            cancel_requested=(False if status == QueueStatus.CANCELLED else entry.cancel_requested),
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
    metadata_update_fn: _MetadataUpdateFn | None = None,
    load_entries_fn: Callable[[Path], list[QueueEntry]] | None = None,
    save_entries_fn: Callable[[Path, Sequence[QueueEntry]], Any] | None = None,
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
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
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
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
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
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
