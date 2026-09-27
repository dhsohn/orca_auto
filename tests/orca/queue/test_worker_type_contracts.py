"""Static regressions: the worker and its queue roots keep concrete ORCA types."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, assert_type

if TYPE_CHECKING:
    from orca_auto.core.queue.types import QueueEntry
    from orca_auto.core.queue.worker.models import ReservedQueueEntry, ReserveStatus
    from orca_auto.orca.config import AppConfig
    from orca_auto.orca.queue import roots
    from orca_auto.orca.queue.models import OrcaRunningJob
    from orca_auto.orca.queue.worker import OrcaQueueWorker

    def worker_contract(worker: OrcaQueueWorker) -> None:
        assert_type(worker.cfg, AppConfig)
        assert_type(worker.admission_root, Path)
        assert_type(roots.queue_roots(worker.cfg), tuple[Path, ...])
        assert_type(roots.peek_next_entry(worker.cfg), tuple[Path, QueueEntry] | None)
        assert_type(worker._reserve_next_entry(), tuple[ReserveStatus, ReservedQueueEntry | None])
        assert_type(worker._running_jobs(), list[tuple[str, OrcaRunningJob]])
