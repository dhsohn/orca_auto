from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, Literal, Protocol, TypeVar

from ..processes import ManagedProcess
from ..types import QueueEntry

T = TypeVar("T")
ReserveStatus = Literal["blocked", "idle", "processed"]


class ProcessBackedJob(Protocol):
    @property
    def process(self) -> ManagedProcess: ...


JobT = TypeVar("JobT", bound=ProcessBackedJob)


@dataclass(frozen=True)
class SlotFillResult:
    status: ReserveStatus
    started: int


@dataclass(frozen=True)
class ReservedQueueEntry(Generic[T]):
    queue_root: Path
    entry: T
    admission_token: str


@dataclass
class BackgroundRunningJob:
    queue_root: Path
    entry: QueueEntry
    process: ManagedProcess
    admission_token: str
    cancel_requested: bool = False
    started_at: float = field(default_factory=time.monotonic)


__all__ = [
    "BackgroundRunningJob",
    "ReservedQueueEntry",
    "SlotFillResult",
]
