from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from orca_auto.core.admission import read_active_slot_count
from orca_auto.core.config.schema import RuntimeAdmissionMixin

from ..deferral import queue_entry_admission_is_deferred
from ..priority import normalize_queue_priority
from ..publication import queue_entry_is_claimable
from ..types import QueueEntry, QueueStatus
from .models import ReservedQueueEntry, ReserveStatus


class WorkerConfig(Protocol):
    @property
    def runtime(self) -> RuntimeAdmissionMixin: ...


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


def select_next_claimable_entry(
    entries: Iterable[QueueEntry],
    *,
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None,
) -> QueueEntry | None:
    """Pick the row a by-id dequeue would claim from one queue listing.

    Only a pending, uncancelled, published row whose admission is not deferred
    and that ``accept_entry_fn`` admits is eligible; rows behind a skipped one
    stay eligible. Among eligible rows the lowest priority value wins and,
    within one priority, the row position: rows are only ever appended under
    the queue lock, so the position is the arrival order. The wall-clock
    ``enqueued_at`` is not monotonic (WSL2 skew corrections step it backwards),
    so it must not reorder same-priority dispatch.
    """
    champion: QueueEntry | None = None
    champion_key: tuple[int, int] | None = None
    for index, entry in enumerate(entries):
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
            continue
        key = (normalize_queue_priority(getattr(entry, "priority", None)), index)
        if champion_key is None or key < champion_key:
            champion_key = key
            champion = entry
    return champion


def reserve_dequeued_entry(
    *,
    has_capacity_fn: Callable[[], bool],
    peek_next_fn: Callable[[], tuple[Path, QueueEntry] | None],
    reserve_slot_fn: Callable[[], str | None],
    dequeue_next_fn: Callable[[], tuple[Path, QueueEntry] | None],
    release_slot_fn: Callable[[str], object],
) -> tuple[ReserveStatus, ReservedQueueEntry | None]:
    """Reserve an admission slot, then claim the previewed row under it.

    The slot is reserved before the dequeue so a claimed row always holds
    capacity; a claim that is lost or raises releases the slot again.
    """
    # Read before writing, and read the cheap thing first: a full pool is one
    # lock-free admission read, and an empty queue is a queue listing, while
    # an admission reservation is a durable write to the shared slot file that
    # an idle worker must not pay (twice, with the release) on every poll. The
    # slot still comes before the dequeue so a claimed row always holds
    # capacity; a preview that loses the race simply releases the slot again.
    if not has_capacity_fn():
        return "blocked", None
    if peek_next_fn() is None:
        return "idle", None

    admission_token = reserve_slot_fn()
    if admission_token is None:
        return "blocked", None

    try:
        dequeued = dequeue_next_fn()
    except Exception:
        release_slot_fn(admission_token)
        raise
    if dequeued is None:
        release_slot_fn(admission_token)
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
    "WorkerConfig",
    "admission_has_capacity",
    "reserve_dequeued_entry",
    "resolve_admission_root",
    "select_next_claimable_entry",
]
