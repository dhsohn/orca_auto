from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class SleepTimer(Protocol):
    def sleep(self, seconds: float) -> None: ...


class QueueEntrySelector(Protocol):
    """Preview or claim the next row, passing over rows ``skip_entry_fn`` names.

    A runtime can honor the filter only when it claims rows by id; one that
    claims its root's head row ignores it.
    """

    def __call__(
        self,
        cfg: Any,
        /,
        *,
        skip_entry_fn: Callable[[Any], bool] | None = None,
    ) -> tuple[Path, Any] | None: ...


AdmissionCapacityCheck = Callable[[Any], bool]
AdmissionReserver = Callable[[Any], str | None]


class SlotReleaser(Protocol):
    def __call__(self, admission_root: str | Path, admission_token: str, /) -> object: ...


class DequeuedEntryReserver(Protocol):
    def __call__(
        self,
        cfg: Any,
        *,
        admission_root: str | Path,
        has_capacity_fn: AdmissionCapacityCheck,
        peek_next_fn: Callable[[Any], tuple[Path, Any] | None],
        reserve_slot_fn: AdmissionReserver,
        dequeue_next_fn: Callable[[Any], tuple[Path, Any] | None],
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
    has_admission_capacity: AdmissionCapacityCheck
    peek_next_entry: QueueEntrySelector
    dequeue_next_entry: QueueEntrySelector
    try_reserve_admission_slot: AdmissionReserver


__all__ = [
    "AdmissionCapacityCheck",
    "ChildQueueWorkerDeps",
    "BackgroundJobProcessStarter",
    "DequeuedEntryReserver",
    "QueueEntrySelector",
    "SleepTimer",
    "SlotReleaser",
]
