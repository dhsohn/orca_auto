from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EngineWorkerLifecycle:
    build_context: Callable[[Any, Any], Any]
    mark_running: Callable[[Any, Any], None]
    run_job: Callable[[Any, Any, Path], Any]
    finalize_entry: Callable[[Any, Any, Any, Path], Any]
    build_outcome: Callable[[Any, Any, Any], Any]
    check_shutdown: Callable[[Any], None] | None = None


def run_engine_worker_lifecycle(
    cfg: Any,
    entry: Any,
    *,
    queue_root: Path | None,
    lifecycle: EngineWorkerLifecycle,
) -> Any:
    active_queue_root = queue_root or Path(str(cfg.runtime.allowed_root)).expanduser().resolve()
    context = lifecycle.build_context(cfg, entry)
    if lifecycle.check_shutdown is not None:
        lifecycle.check_shutdown(context)
    lifecycle.mark_running(cfg, context)
    if lifecycle.check_shutdown is not None:
        lifecycle.check_shutdown(context)

    result = lifecycle.run_job(cfg, context, active_queue_root)
    organized_output_dir = lifecycle.finalize_entry(
        cfg,
        context,
        result,
        active_queue_root,
    )
    return lifecycle.build_outcome(context, result, organized_output_dir)


def run_engine_worker_entry(
    cfg: Any,
    entry: Any,
    *,
    queue_root: Path | None,
    build_context: Callable[[Any, Any], Any],
    mark_running: Callable[[Any, Any], None],
    run_job: Callable[[Any, Any, Path], Any],
    finalize_entry: Callable[[Any, Any, Any, Path], Any],
    build_outcome: Callable[[Any, Any, Any], Any],
    check_shutdown: Callable[[Any], None] | None = None,
) -> Any:
    return run_engine_worker_lifecycle(
        cfg,
        entry,
        queue_root=queue_root,
        lifecycle=EngineWorkerLifecycle(
            build_context=build_context,
            mark_running=mark_running,
            run_job=run_job,
            finalize_entry=finalize_entry,
            build_outcome=build_outcome,
            check_shutdown=check_shutdown,
        ),
    )


__all__ = ["EngineWorkerLifecycle", "run_engine_worker_entry", "run_engine_worker_lifecycle"]
