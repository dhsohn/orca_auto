from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic

from ..dependencies import ConfigT, QueueEntryDequeuer
from ..types import QueueEntry
from ..worker import (
    admission_has_capacity,
    dequeue_next_across_roots,
    peek_next_across_roots,
    read_worker_pid_file,
    resolve_admission_root,
)
from ..worker import (
    queue_entry_by_id as _queue_entry_by_id,
)


@dataclass(frozen=True)
class EngineQueueRuntime(Generic[ConfigT]):
    runtime_roots_for_cfg: Callable[[ConfigT], tuple[Path, ...]]
    list_queue: Callable[[str | Path], list[QueueEntry]]
    dequeue_next: Callable[[Path], QueueEntry | None]
    worker_pid_file_name: str
    dequeue_entry_if_pending: QueueEntryDequeuer[QueueEntry] | None = None
    accept_entry_fn: Callable[[QueueEntry], bool] | None = None
    queue_entry_by_id_fn: Callable[[str | Path, str], QueueEntry | None] | None = None

    def queue_roots(self, cfg: ConfigT) -> tuple[Path, ...]:
        return tuple(self.runtime_roots_for_cfg(cfg))

    def _existing_queue_roots(self, cfg: ConfigT) -> tuple[Path, ...]:
        return tuple(root for root in self.queue_roots(cfg) if root.expanduser().exists())

    def _accept_unless_skipped(
        self,
        skip_entry_fn: Callable[[QueueEntry], bool] | None,
    ) -> Callable[[QueueEntry], bool] | None:
        if skip_entry_fn is None or self.dequeue_entry_if_pending is None:
            # Without a by-id dequeue the root claims its own head row, which a
            # selection filter cannot steer. Engine definitions always supply a
            # by-id dequeue, so only hand-built runtimes take this branch.
            return self.accept_entry_fn
        accept_entry_fn = self.accept_entry_fn

        def accept(entry: QueueEntry) -> bool:
            if accept_entry_fn is not None and not accept_entry_fn(entry):
                return False
            return not skip_entry_fn(entry)

        return accept

    def peek_next_entry(
        self,
        cfg: ConfigT,
        /,
        *,
        skip_entry_fn: Callable[[QueueEntry], bool] | None = None,
    ) -> tuple[Path, QueueEntry] | None:
        return peek_next_across_roots(
            self._existing_queue_roots(cfg),
            list_queue_fn=self.list_queue,
            select_all_rows=self.dequeue_entry_if_pending is not None,
            accept_entry_fn=self._accept_unless_skipped(skip_entry_fn),
        )

    def has_admission_capacity(self, cfg: ConfigT) -> bool:
        return admission_has_capacity(cfg)

    def queue_entries_with_roots(
        self,
        cfg: ConfigT,
        *,
        list_queue_fn: Callable[[str | Path], list[QueueEntry]] | None = None,
    ) -> list[tuple[Path, QueueEntry]]:
        queue_lister = list_queue_fn or self.list_queue
        return [
            (root, entry)
            for root in self._existing_queue_roots(cfg)
            for entry in queue_lister(root)
            if self.accepts_entry(entry)
        ]

    def dequeue_next_entry(
        self,
        cfg: ConfigT,
        /,
        *,
        skip_entry_fn: Callable[[QueueEntry], bool] | None = None,
    ) -> tuple[Path, QueueEntry] | None:
        return dequeue_next_across_roots(
            self._existing_queue_roots(cfg),
            list_queue_fn=self.list_queue,
            dequeue_next_fn=self.dequeue_next,
            dequeue_entry_fn=self.dequeue_entry_if_pending,
            accept_entry_fn=self._accept_unless_skipped(skip_entry_fn),
        )

    def queue_entry_by_id(self, queue_root: Path | str, queue_id: str) -> QueueEntry | None:
        if self.queue_entry_by_id_fn is not None:
            entry = self.queue_entry_by_id_fn(queue_root, queue_id)
        else:
            entry = _queue_entry_by_id(
                queue_root,
                queue_id,
                list_queue_fn=self.list_queue,
            )
        if entry is not None and not self.accepts_entry(entry):
            return None
        return entry

    def accepts_entry(self, entry: QueueEntry) -> bool:
        return bool(self.accept_entry_fn is None or self.accept_entry_fn(entry))

    def admission_root(self, cfg: ConfigT) -> str:
        return resolve_admission_root(cfg)

    def read_worker_pid(self, allowed_root: Path) -> int | None:
        return read_worker_pid_file(allowed_root, self.worker_pid_file_name)


__all__ = ["EngineQueueRuntime"]
