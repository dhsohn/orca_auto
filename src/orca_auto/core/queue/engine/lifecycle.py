from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

FinalizedT = TypeVar("FinalizedT")
OutcomeT = TypeVar("OutcomeT")


@dataclass(frozen=True)
class EngineWorkerLifecycle(Generic[FinalizedT, OutcomeT]):
    build_context: Callable[[Any, Any], Any]
    mark_running: Callable[[Any, Any], None]
    run_job: Callable[[Any, Any, Path], Any]
    finalize_entry: Callable[[Any, Any, Any, Path], FinalizedT]
    build_outcome: Callable[[Any, Any, FinalizedT], OutcomeT]
    check_shutdown: Callable[[Any], None] | None = None


def run_engine_worker_lifecycle(
    cfg: Any,
    entry: Any,
    *,
    queue_root: Path | None,
    lifecycle: EngineWorkerLifecycle[FinalizedT, OutcomeT],
) -> OutcomeT:
    active_queue_root = queue_root or Path(str(cfg.runtime.allowed_root)).expanduser().resolve()
    context = lifecycle.build_context(cfg, entry)
    if lifecycle.check_shutdown is not None:
        lifecycle.check_shutdown(context)
    lifecycle.mark_running(cfg, context)
    if lifecycle.check_shutdown is not None:
        lifecycle.check_shutdown(context)

    result = lifecycle.run_job(cfg, context, active_queue_root)
    sync_result = lifecycle.finalize_entry(
        cfg,
        context,
        result,
        active_queue_root,
    )
    return lifecycle.build_outcome(context, result, sync_result)


__all__ = ["EngineWorkerLifecycle", "run_engine_worker_lifecycle"]
