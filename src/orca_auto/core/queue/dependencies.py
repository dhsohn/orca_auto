from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class SleepTimer(Protocol):
    def sleep(self, seconds: float) -> None: ...


QueueEntryDequeuer = Callable[[Any], tuple[Path, Any] | None]
AdmissionReserver = Callable[[Any], str | None]


class SlotReleaser(Protocol):
    def __call__(self, admission_root: str | Path, admission_token: str, /) -> object: ...


class DequeuedEntryReserver(Protocol):
    def __call__(
        self,
        cfg: Any,
        *,
        admission_root: str | Path,
        reserve_slot_fn: AdmissionReserver,
        dequeue_next_fn: QueueEntryDequeuer,
        release_slot_fn: SlotReleaser,
    ) -> tuple[str, Any | None]: ...


class BackgroundJobProcessStarter(Protocol):
    def __call__(
        self,
        *,
        config_path: str,
        queue_root: Path,
        entry: Any,
        admission_token: str,
    ) -> Any: ...


@dataclass(frozen=True)
class ChildQueueWorkerDeps:
    poll_interval_seconds: int
    time: SleepTimer
    admission_root: Callable[[Any], str]
    start_background_job_process: BackgroundJobProcessStarter
    release_slot: SlotReleaser
    reserve_dequeued_entry: DequeuedEntryReserver
    dequeue_next_entry: QueueEntryDequeuer
    try_reserve_admission_slot: AdmissionReserver


__all__ = [
    "ChildQueueWorkerDeps",
    "BackgroundJobProcessStarter",
    "DequeuedEntryReserver",
    "QueueEntryDequeuer",
    "SleepTimer",
    "SlotReleaser",
]
