from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ..processes import ManagedProcess
from ..types import QueueEntry

ReserveStatus = Literal["blocked", "idle", "processed"]


class ProcessBackedJob(Protocol):
    """What the worker loop needs from a tracked job: the child it supervises."""

    @property
    def process(self) -> ManagedProcess: ...


@dataclass(frozen=True)
class SlotFillResult:
    status: ReserveStatus
    started: int


@dataclass(frozen=True)
class ReservedQueueEntry:
    """A claimed queue row together with the admission slot reserved before the claim."""

    queue_root: Path
    entry: QueueEntry
    admission_token: str


__all__ = [
    "ProcessBackedJob",
    "ReserveStatus",
    "ReservedQueueEntry",
    "SlotFillResult",
]
