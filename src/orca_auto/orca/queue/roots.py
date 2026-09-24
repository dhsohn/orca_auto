"""The ORCA worker's queue roots and the row selection bound to the ORCA identity filter.

Every function here works on the runtime roots of one configuration, lists
rows through the ORCA adapter and admits only rows with the complete ORCA
engine identity. Rows are claimed by id with the previewed row as the
``expected_entry`` fence, never by head-of-queue position.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from orca_auto.core.indexing.roots import runtime_roots_for_cfg
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.queue.worker.admission import select_next_claimable_entry

from ..config import AppConfig
from .adapter import dequeue_entry_if_pending, get_entry_by_id, list_queue
from .identity import own_engine_accept_entry

accept_orca_entry = own_engine_accept_entry("orca")


def queue_roots(cfg: AppConfig) -> tuple[Path, ...]:
    return tuple(runtime_roots_for_cfg(cfg))


def existing_queue_roots(cfg: AppConfig) -> tuple[Path, ...]:
    """The queue roots that exist on disk; listing never creates a root."""
    return tuple(root for root in queue_roots(cfg) if root.expanduser().exists())


def queue_entries_with_roots(cfg: AppConfig) -> list[tuple[Path, QueueEntry]]:
    return [
        (root, entry)
        for root in existing_queue_roots(cfg)
        for entry in list_queue(root)
        if accept_orca_entry(entry)
    ]


def _accept_unless_skipped(
    skip_entry_fn: Callable[[QueueEntry], bool] | None,
) -> Callable[[QueueEntry], bool]:
    if skip_entry_fn is None:
        return accept_orca_entry
    # The engine filter runs first, so a foreign row never reaches the skip predicate.
    return lambda entry: accept_orca_entry(entry) and not skip_entry_fn(entry)


def peek_next_entry(
    cfg: AppConfig,
    *,
    skip_entry_fn: Callable[[QueueEntry], bool] | None = None,
) -> tuple[Path, QueueEntry] | None:
    """Read-only preview of what ``dequeue_next_entry`` would claim.

    The worker consults this before reserving an admission slot so an idle
    poll never rewrites the admission file. The preview is never stricter than
    the dequeue: a row it selects may still be lost to a concurrent claim or
    cancellation, and the dequeue then reports that.
    """
    accept = _accept_unless_skipped(skip_entry_fn)
    for root in existing_queue_roots(cfg):
        selected = select_next_claimable_entry(list_queue(root), accept_entry_fn=accept)
        if selected is not None:
            return root, selected
    return None


def dequeue_next_entry(
    cfg: AppConfig,
    *,
    skip_entry_fn: Callable[[QueueEntry], bool] | None = None,
) -> tuple[Path, QueueEntry] | None:
    """Claim the previewed row by id, fenced on the previewed generation."""
    selected = peek_next_entry(cfg, skip_entry_fn=skip_entry_fn)
    if selected is None:
        return None
    root, entry = selected
    queue_id = str(getattr(entry, "queue_id", "")).strip()
    if not queue_id:
        return None
    claimed = dequeue_entry_if_pending(root, queue_id, expected_entry=entry)
    if claimed is None:
        return None
    return root, claimed


def queue_entry_by_id(queue_root: Path | str, queue_id: str) -> QueueEntry | None:
    """One ORCA row by id; a row without the ORCA identity is reported as absent."""
    entry = get_entry_by_id(Path(queue_root), queue_id)
    if entry is not None and not accept_orca_entry(entry):
        return None
    return entry


__all__ = [
    "accept_orca_entry",
    "dequeue_next_entry",
    "existing_queue_roots",
    "peek_next_entry",
    "queue_entries_with_roots",
    "queue_entry_by_id",
    "queue_roots",
]
