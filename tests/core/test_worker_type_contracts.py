# mypy: warn-unused-ignores
"""Static regressions: losing callback precision makes the ignores below fail."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, assert_type

if TYPE_CHECKING:
    from orca_auto.core.engines.definitions import EngineDefinition, WorkerChildRunner
    from orca_auto.core.queue.dependencies import (
        ChildQueueWorkerDeps,
        QueueEntryDequeuer,
        QueueEntryFailureMarker,
    )
    from orca_auto.core.queue.types import QueueEntry
    from orca_auto.orca.config import AppConfig
    from orca_auto.orca.engine import ENGINE_DEFINITION
    from orca_auto.orca.queue.models import OrcaRunningJob
    from orca_auto.orca.queue.worker import OrcaQueueWorker

    def missing_generation(root: Path, queue_id: str) -> QueueEntry | None:
        return None

    def missing_failure_generation(root: Path, queue_id: str, *, error: str) -> bool:
        return True

    invalid_failure_marker: QueueEntryFailureMarker = missing_failure_generation  # type: ignore[assignment]

    def missing_child_arguments() -> int:
        return 0

    invalid_dequeuer: QueueEntryDequeuer[QueueEntry] = missing_generation  # type: ignore[assignment]
    invalid_runner: WorkerChildRunner = missing_child_arguments  # type: ignore[assignment]

    def worker_contract(worker: OrcaQueueWorker) -> None:
        assert_type(ENGINE_DEFINITION, EngineDefinition[AppConfig])
        assert_type(ENGINE_DEFINITION.load_config("config.yaml"), AppConfig)
        assert_type(worker.cfg, AppConfig)
        assert_type(worker.deps, ChildQueueWorkerDeps[AppConfig])
        assert_type(worker._running_jobs(), list[tuple[str, OrcaRunningJob]])
