from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar

from orca_auto.core.config.schema import RuntimeAdmissionMixin

from .processes import ManagedProcess
from .types import QueueEntry


class WorkerConfig(Protocol):
    @property
    def runtime(self) -> RuntimeAdmissionMixin: ...


ConfigT = TypeVar("ConfigT", bound=WorkerConfig)
ConfigT_contra = TypeVar("ConfigT_contra", bound=WorkerConfig, contravariant=True)
EntryT = TypeVar("EntryT")


class QueueEntryDequeuer(Protocol[EntryT]):
    def __call__(
        self,
        root: Path,
        queue_id: str,
        /,
        *,
        expected_entry: EntryT | None,
    ) -> EntryT | None: ...


class QueueEntrySelector(Protocol[ConfigT_contra]):
    def __call__(
        self,
        cfg: ConfigT_contra,
        /,
        *,
        skip_entry_fn: Callable[[QueueEntry], bool] | None = None,
    ) -> tuple[Path, QueueEntry] | None: ...


class QueueEntryFailureMarker(Protocol):
    def __call__(
        self,
        root: Path,
        queue_id: str,
        /,
        *,
        error: str,
        expected_entry: QueueEntry,
    ) -> object: ...


class BackgroundJobProcessStarter(Protocol):
    def __call__(
        self,
        *,
        config_path: str,
        queue_root: Path,
        entry: QueueEntry,
        admission_token: str,
    ) -> ManagedProcess: ...


@dataclass(frozen=True)
class ChildQueueWorkerDeps(Generic[ConfigT]):
    poll_interval_seconds: float
    sleep: Callable[[float], None]
    start_background_job_process: BackgroundJobProcessStarter
    release_slot: Callable[[str | Path, str], object]
    has_admission_capacity: Callable[[ConfigT], bool]
    peek_next_entry: QueueEntrySelector[ConfigT]
    dequeue_next_entry: QueueEntrySelector[ConfigT]
    try_reserve_admission_slot: Callable[[ConfigT], str | None]
