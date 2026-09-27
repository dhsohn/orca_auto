"""The ORCA worker's one queue root and the row selection bound to the ORCA identity filter.

The queue root is the resolved ``runtime.allowed_root``. Rows are listed
through the ORCA adapter and only rows with the complete ORCA engine identity
are admitted. Rows are claimed by id with the previewed row as the
``expected_entry`` fence, never by head-of-queue position.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.queue.worker.admission import select_next_claimable_entry

from ..config import AppConfig
from .adapter import dequeue_entry_if_pending, get_entry_by_id, list_queue
from .identity import own_engine_accept_entry

accept_orca_entry = own_engine_accept_entry("orca")


def queue_root(cfg: AppConfig) -> Path:
    return Path(cfg.runtime.allowed_root).expanduser().resolve()


def list_orca_rows(cfg: AppConfig) -> list[QueueEntry]:
    """ORCA rows at the queue root; a missing root lists nothing and is not created."""
    root = queue_root(cfg)
    if not root.exists():
        return []
    return [entry for entry in list_queue(root) if accept_orca_entry(entry)]


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
    # The engine filter runs first, so a foreign row never reaches the skip predicate.
    selected = select_next_claimable_entry(
        list_orca_rows(cfg),
        accept_entry_fn=(None if skip_entry_fn is None else lambda entry: not skip_entry_fn(entry)),
    )
    if selected is None:
        return None
    return queue_root(cfg), selected


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
    "list_orca_rows",
    "peek_next_entry",
    "queue_entry_by_id",
    "queue_root",
]
