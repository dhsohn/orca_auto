from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ..types import QueueEntry


@dataclass
class ChildWorkerShutdownController:
    requested: bool = False

    def request(self) -> None:
        self.requested = True

    def is_requested(self) -> bool:
        return self.requested


def find_queue_entry_by_id(
    queue_root: str | Path,
    queue_id: str,
    *,
    list_queue_fn: Callable[[Path], Iterable[QueueEntry]],
) -> QueueEntry | None:
    for entry in list_queue_fn(Path(queue_root)):
        if entry.queue_id == queue_id:
            return entry
    return None


__all__ = ["ChildWorkerShutdownController", "find_queue_entry_by_id"]
