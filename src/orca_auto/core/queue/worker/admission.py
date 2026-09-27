from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from orca_auto.core.admission import read_active_slot_count

from ..priority import normalize_queue_priority
from ..store import claimable_pending
from ..types import QueueEntry


def admission_has_capacity(admission_root: Path, limit: int) -> bool:
    """Read-only check that the admission store could admit one more slot.

    Counts live slots without locking or rewriting the admission file, using
    the same limit the reservation itself enforces. A worker whose store is
    full stops here, before it lists the queue.
    """
    return read_active_slot_count(admission_root) < limit


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
        if not claimable_pending(entry):
            continue
        if accept_entry_fn is not None and not accept_entry_fn(entry):
            continue
        key = (normalize_queue_priority(entry.priority), index)
        if champion_key is None or key < champion_key:
            champion_key = key
            champion = entry
    return champion


__all__ = [
    "admission_has_capacity",
    "select_next_claimable_entry",
]
