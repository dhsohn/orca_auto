from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class SlotFillResult:
    status: str
    started: int


@dataclass(frozen=True)
class ReservedQueueEntry(Generic[T]):
    queue_root: Path
    entry: T
    admission_token: str


@dataclass
class BackgroundRunningJob:
    queue_root: Path
    entry: Any
    process: Any
    admission_token: str
    cancel_requested: bool = False
    started_at: float = field(default_factory=time.monotonic)


@dataclass
class EngineRunningJob:
    queue_id: str
    reaction_dir: str
    process: Any
    admission_token: str
    task_id: str | None = None
    started_at: float = field(default_factory=time.monotonic)


__all__ = [
    "BackgroundRunningJob",
    "EngineRunningJob",
    "ReservedQueueEntry",
    "SlotFillResult",
]
