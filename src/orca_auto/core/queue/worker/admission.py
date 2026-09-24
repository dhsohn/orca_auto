from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from orca_auto.core.admission import read_active_slot_count

from ..child.execution import find_queue_entry_by_id
from ..deferral import queue_entry_admission_is_deferred
from ..dependencies import ConfigT, QueueEntryDequeuer, WorkerConfig
from ..priority import normalize_queue_priority
from ..publication import queue_entry_is_claimable
from ..types import QueueEntry, QueueStatus
from .models import ReservedQueueEntry, ReserveStatus

T = TypeVar("T")


def resolve_admission_root(cfg: WorkerConfig) -> str:
    """Return the shared admission root resolved by the runtime configuration."""
    return str(cfg.runtime.resolved_admission_root)


def admission_has_capacity(cfg: WorkerConfig) -> bool:
    """Read-only check that the shared pool could admit one more slot.

    Counts live slots without locking or rewriting the admission file, using
    the same limit the reservation itself enforces. A worker whose pool is
    full stops here, before it lists any queue root.
    """
    limit = int(cfg.runtime.resolved_admission_limit)
    return read_active_slot_count(resolve_admission_root(cfg)) < limit


def _select_next_claimable_entry(
    roots: tuple[Path, ...],
    *,
    list_queue_fn: Callable[[Path], list[T]],
    select_all_rows: bool,
    accept_entry_fn: Callable[[T], bool] | None,
) -> tuple[Path, T] | None:
    """Pick the row a dequeue would claim across *roots* without mutating anything.

    ``select_all_rows`` is true when the caller can dequeue a specific row by
    id; otherwise each root's own head-of-queue rule decides, so only the first
    eligible row per root is considered.
    """
    selected_root: Path | None = None
    selected_entry: T | None = None
    selected_key: tuple[int, str, int] | None = None

    for root_index, root in enumerate(roots):
        champion_entry: T | None = None
        champion_key: tuple[int, int] | None = None
        for entry_index, entry in enumerate(list_queue_fn(root)):
            status_value = getattr(getattr(entry, "status", None), "value", None)
            status = str(status_value).strip().lower()
            if status != QueueStatus.PENDING.value or getattr(entry, "cancel_requested", False):
                continue
            if not queue_entry_is_claimable(entry):
                continue
            if queue_entry_admission_is_deferred(entry):
                # Waiting for a resource, not for queue order: rows behind it stay eligible.
                continue
            if accept_entry_fn is not None and not accept_entry_fn(entry):
                if not select_all_rows:
                    break
                continue
            # Within one queue file the row position is the arrival order:
            # rows are only ever appended under the queue lock. The wall-clock
            # enqueued_at is not monotonic (WSL2 skew corrections step it
            # backwards), so it must not reorder same-priority dispatch.
            key = (
                normalize_queue_priority(getattr(entry, "priority", None)),
                entry_index,
            )
            if champion_key is None or key < champion_key:
                champion_key = key
                champion_entry = entry
            if not select_all_rows:
                break
        if champion_entry is None or champion_key is None:
            continue
        # Different queue files share no arrival order, so the wall clock
        # remains the cross-root fairness comparator.
        root_key = (
            champion_key[0],
            str(getattr(champion_entry, "enqueued_at", "")),
            root_index,
        )
        if selected_key is None or root_key < selected_key:
            selected_key = root_key
            selected_root = root
            selected_entry = champion_entry

    if selected_root is None or selected_entry is None:
        return None
    return selected_root, selected_entry


def peek_next_across_roots(
    roots: tuple[Path, ...],
    *,
    list_queue_fn: Callable[[Path], list[T]],
    select_all_rows: bool,
    accept_entry_fn: Callable[[T], bool] | None = None,
) -> tuple[Path, T] | None:
    """Read-only preview of ``dequeue_next_across_roots`` for the same roots.

    The worker consults this before reserving an admission slot so an idle
    poll never rewrites the admission file. The preview is never stricter than
    the dequeue: a row it selects may still be lost to a concurrent claim or
    cancellation, and the dequeue then reports that. On the single-root fast
    path the root's own ``dequeue_next`` owns the eligibility rule, so the
    preview only requires what every queue store requires — a pending,
    uncancelled row whose admission is not deferred — and leaves the rest to
    the dequeue.
    """
    if len(roots) == 1 and accept_entry_fn is None:
        for entry in list_queue_fn(roots[0]):
            status_value = getattr(getattr(entry, "status", None), "value", None)
            status = str(status_value).strip().lower()
            if (
                status == QueueStatus.PENDING.value
                and not getattr(entry, "cancel_requested", False)
                and not queue_entry_admission_is_deferred(entry)
            ):
                return roots[0], entry
        return None
    return _select_next_claimable_entry(
        roots,
        list_queue_fn=list_queue_fn,
        select_all_rows=select_all_rows,
        accept_entry_fn=accept_entry_fn,
    )


def dequeue_next_across_roots(
    roots: tuple[Path, ...],
    *,
    list_queue_fn: Callable[[Path], list[T]],
    dequeue_next_fn: Callable[[Path], T | None],
    dequeue_entry_fn: QueueEntryDequeuer[T] | None = None,
    accept_entry_fn: Callable[[T], bool] | None = None,
) -> tuple[Path, T] | None:
    if len(roots) == 1 and accept_entry_fn is None:
        entry = dequeue_next_fn(roots[0])
        if entry is None:
            return None
        return roots[0], entry

    selected = _select_next_claimable_entry(
        roots,
        list_queue_fn=list_queue_fn,
        select_all_rows=dequeue_entry_fn is not None,
        accept_entry_fn=accept_entry_fn,
    )
    if selected is None:
        return None
    selected_root, selected_entry = selected
    selected_queue_id = str(getattr(selected_entry, "queue_id", "")).strip()

    if dequeue_entry_fn is not None and selected_queue_id:
        entry = dequeue_entry_fn(
            selected_root,
            selected_queue_id,
            expected_entry=selected_entry,
        )
    else:
        entry = dequeue_next_fn(selected_root)
    if entry is None:
        return None
    return selected_root, entry


def queue_entry_by_id(
    queue_root: str | Path,
    queue_id: str,
    *,
    list_queue_fn: Callable[[str | Path], list[QueueEntry]],
) -> QueueEntry | None:
    return find_queue_entry_by_id(
        queue_root,
        queue_id,
        list_queue_fn=list_queue_fn,
    )


def reserve_dequeued_entry(
    cfg: ConfigT,
    *,
    admission_root: str | Path,
    has_capacity_fn: Callable[[ConfigT], bool],
    peek_next_fn: Callable[[ConfigT], tuple[Path, T] | None],
    reserve_slot_fn: Callable[[ConfigT], str | None],
    dequeue_next_fn: Callable[[ConfigT], tuple[Path, T] | None],
    release_slot_fn: Callable[[str | Path, str], object],
) -> tuple[ReserveStatus, ReservedQueueEntry[T] | None]:
    # Read before writing, and read the cheap thing first: a full pool is one
    # lock-free admission read, and an empty queue is a queue listing, while
    # an admission reservation is a durable write to the shared slot file that
    # an idle worker must not pay (twice, with the release) on every poll. The
    # slot still comes before the dequeue so a claimed row always holds
    # capacity; a preview that loses the race simply releases the slot again.
    if not has_capacity_fn(cfg):
        return "blocked", None
    if peek_next_fn(cfg) is None:
        return "idle", None

    admission_token = reserve_slot_fn(cfg)
    if admission_token is None:
        return "blocked", None

    try:
        dequeued = dequeue_next_fn(cfg)
    except Exception:
        release_slot_fn(admission_root, admission_token)
        raise
    if dequeued is None:
        release_slot_fn(admission_root, admission_token)
        return "idle", None

    queue_root, entry = dequeued
    return (
        "processed",
        ReservedQueueEntry(
            queue_root=queue_root,
            entry=entry,
            admission_token=admission_token,
        ),
    )


__all__ = [
    "admission_has_capacity",
    "dequeue_next_across_roots",
    "peek_next_across_roots",
    "queue_entry_by_id",
    "reserve_dequeued_entry",
    "resolve_admission_root",
]
