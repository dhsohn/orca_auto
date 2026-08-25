"""ORCA queue-worker composition under external supervision."""

from __future__ import annotations

import copy
import logging
import time
from pathlib import Path
from typing import Any

from orca_auto.core.admission import (
    activate_reserved_slot,
    recover_slot_engine_process,
    release_slot,
    reserve_slot,
)
from orca_auto.core.config.schema import resolved_admission_limit
from orca_auto.core.engine_catalog import get_engine_catalog_entry
from orca_auto.core.engines.queue_worker import (
    EngineQueueWorker,
    build_runtime_engine_queue_worker,
)
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.queue.worker import EngineRunningJob as _RunningJob
from orca_auto.core.queue.worker import ManagedProcess as _ManagedProcess
from orca_auto.core.queue.worker import start_background_process, terminate_process_group
from orca_auto.orca.worker_execution import (
    BackgroundRunJobProcess,
    build_worker_child_command,
)

from ..config import AppConfig
from ..engine import ENGINE_DEFINITION, ENGINE_RUNTIME
from . import cancellation, publication_repair, replay, worker_runtime
from .adapter import (
    get_cancel_requested,
    list_queue,
    mark_failed,
    queue_entry_id,
    queue_entry_reaction_dir,
    queue_entry_task_id,
    worker_log_path,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT = 4
POLL_INTERVAL_SECONDS = 5
WORKER_SHUTDOWN_GRACE_SECONDS = 10.0

_ENGINE_RUNTIME = ENGINE_RUNTIME


def _default_config_path() -> str:
    return ""


def _list_queue_for_runtime(root: str | Path) -> list[QueueEntry]:
    return list_queue(Path(root))


def queue_roots(cfg: AppConfig) -> tuple[Path, ...]:
    return _ENGINE_RUNTIME.queue_roots(cfg)


def queue_entries_with_roots(cfg: AppConfig) -> list[tuple[Path, Any]]:
    return _ENGINE_RUNTIME.queue_entries_with_roots(
        cfg,
        list_queue_fn=_list_queue_for_runtime,
    )


def _queue_worker_deps() -> Any:
    return _ENGINE_RUNTIME.child_worker_deps(
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
        time_module=time,
        release_slot_fn=lambda root, token: release_slot(root, token),
        start_background_job_process_fn=lambda **kwargs: _start_background_job_process(**kwargs),
        try_reserve_admission_slot_fn=lambda cfg: _try_reserve_admission_slot(cfg),
    )


def _admission_root_for_cfg(cfg: AppConfig) -> str:
    return _ENGINE_RUNTIME.admission_root(cfg)


def _reserve_orca_worker_slot(root: str | Path, limit: int, **kwargs: Any) -> str | None:
    catalog_entry = get_engine_catalog_entry("orca")
    slot_kwargs = dict(kwargs)
    slot_kwargs["source"] = catalog_entry.admission_source
    slot_kwargs["app_name"] = catalog_entry.app_id
    slot_kwargs["state"] = "reserved"
    slot_kwargs["engine_launch_gated"] = catalog_entry.engine_launch_gated
    return reserve_slot(
        Path(root),
        limit,
        **slot_kwargs,
    )


def _try_reserve_admission_slot(cfg: AppConfig) -> str | None:
    admission_token = _ENGINE_RUNTIME.reserve_admission_slot(
        cfg,
        engine="orca",
        reserve_slot_fn=_reserve_orca_worker_slot,
    )
    if admission_token is None:
        logger.debug(
            "Queue worker admission paused: admission slots are full (admission_limit=%d)",
            cfg.runtime.resolved_admission_limit,
        )
    return admission_token


def _start_background_job_process(
    *,
    config_path: str,
    queue_root: Path,
    entry: QueueEntry,
    admission_token: str,
) -> BackgroundRunJobProcess:
    log_path = str(worker_log_path(queue_root, queue_entry_id(entry)))
    return start_background_process(
        build_worker_child_command(
            config_path=config_path,
            queue_root=queue_root,
            queue_id=queue_entry_id(entry),
            admission_token=admission_token,
        ),
        log_path=log_path,
    )


def _terminate_process(proc: _ManagedProcess) -> bool:
    """Terminate the background run process and escalate if it does not stop."""
    return terminate_process_group(proc)


def _worker_admission_limit(cfg: AppConfig, fallback_max_concurrent: int) -> int:
    raw_limit = cfg.runtime.admission_limit
    if raw_limit in (None, "", 0):
        raw_limit = fallback_max_concurrent
    return resolved_admission_limit(raw_limit, fallback_max_concurrent)


def _worker_config_with_effective_concurrency(
    cfg: AppConfig,
    configured_max: int,
) -> AppConfig:
    if cfg.runtime.admission_limit not in (None, "", 0):
        return cfg
    worker_cfg = copy.copy(cfg)
    worker_cfg.runtime = copy.copy(cfg.runtime)
    worker_cfg.runtime.max_concurrent = configured_max
    return worker_cfg


def _shutdown_running_job(worker: Any, queue_id: str, job: Any) -> None:
    if get_cancel_requested(
        replay.job_queue_root(worker, job),
        queue_id,
        expected_task_id=job.task_id,
    ):
        # A cancel landed before the worker loop could process it proactively. The
        # shared requeue chokepoint would still honor it (mark the entry cancelled
        # instead of requeuing for resume) but skip the terminal side effects --
        # the cancelled run state and the notification. Route it through the same
        # finalize path as a proactive cancel so the user is told it stopped and no
        # stale "running" run state lingers.
        cancellation.cancel_running_job(worker, queue_id, job)
        return

    def terminate_child_and_recover(process: Any) -> bool:
        terminated = replay.terminate_process(process)
        if terminated is True and process.poll() is not None:
            recover_slot_engine_process(worker.admission_root, job.admission_token)
        return terminated

    replay.shutdown_running_job(
        worker,
        queue_id,
        job,
        terminate_process_fn=terminate_child_and_recover,
    )


def _before_shutdown_all(_worker: Any, running_count: int) -> None:
    logger.info("Shutting down %d running job(s)...", running_count)


def _queue_worker_hooks() -> Any:
    return _ENGINE_RUNTIME.child_worker_hooks(
        engine="orca",
        handle_worker_start_error_fn=replay.handle_worker_start_error,
        finalize_completed_job_fn=replay.finalize_completed_job,
        finalize_child_exit_fn=replay.finalize_child_exit,
        reconcile_worker_state_fn=replay.reconcile_worker_state,
        activate_reserved_slot_fn=lambda *args, **kwargs: activate_reserved_slot(*args, **kwargs),
        terminate_process_fn=lambda process: _terminate_process(process),
        mark_failed_fn=lambda *args, **kwargs: mark_failed(*args, **kwargs),
        shutdown_grace_seconds=WORKER_SHUTDOWN_GRACE_SECONDS,
        sleep_fn=lambda seconds: time.sleep(seconds),
        on_worker_process_started_fn=replay.on_worker_process_started,
        shutdown_running_job_fn=_shutdown_running_job,
        before_shutdown_all_fn=_before_shutdown_all,
    )


def _after_orca_worker_init(worker: EngineQueueWorker) -> None:
    worker.admission_limit = _worker_admission_limit(worker.cfg, worker.max_concurrent)
    worker.engine_state = replay.OrcaWorkerReplayState()


def _orca_reserve_gate(worker: EngineQueueWorker) -> tuple[str, Any | None] | None:
    # A failed terminal side effect retains its completed job and durable marker.
    # Gate admission itself (not merely slot release): max_concurrent > 1 and
    # multi-root queues could otherwise start a forced successor in another slot.
    if replay.terminal_replay_blocks_new_generation(worker):
        logger.warning("Queue admission paused until durable ORCA terminal replay completes")
        return "blocked", None
    if not publication_repair.repair_queue_publications(worker):
        logger.warning("Queue admission paused until ORCA queued publication repair succeeds")
        return "blocked", None
    return None


def _before_orca_worker_run(worker: EngineQueueWorker) -> None:
    worker_runtime.before_worker_run(worker)


def _after_orca_worker_run(_worker: EngineQueueWorker) -> None:
    worker_runtime.after_worker_run(_worker)


def _log_orca_worker_interrupt(_worker: EngineQueueWorker) -> None:
    worker_runtime.log_worker_interrupt(_worker)


def _make_orca_running_job(
    _worker: EngineQueueWorker,
    *,
    queue_root: Path,
    entry: Any,
    process: Any,
    admission_token: str,
) -> _RunningJob:
    return worker_runtime.make_running_job(
        queue_root=queue_root,
        entry=entry,
        process=process,
        admission_token=admission_token,
        queue_entry_id_fn=queue_entry_id,
        queue_entry_reaction_dir_fn=queue_entry_reaction_dir,
        queue_entry_task_id_fn=queue_entry_task_id,
        running_job_cls=_RunningJob,
    )


def _check_orca_cancel_requests(worker: EngineQueueWorker) -> None:
    worker_runtime.check_cancel_requests(
        worker,
        get_cancel_requested_fn=get_cancel_requested,
        job_queue_root_fn=replay.job_queue_root,
        cancel_running_job_fn=cancellation.cancel_running_job,
    )


def QueueWorker(
    cfg: AppConfig,
    config_path: str,
    *,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> EngineQueueWorker:
    configured_max = max(1, int(max_concurrent))
    worker_cfg = _worker_config_with_effective_concurrency(cfg, configured_max)
    worker = build_runtime_engine_queue_worker(
        worker_cfg,
        config_path=config_path,
        default_config_path=_default_config_path,
        engine="orca",
        max_concurrent=configured_max,
        deps=_queue_worker_deps(),
        hooks=_queue_worker_hooks(),
        worker_pid_file_name=ENGINE_DEFINITION.queue_functions.worker_pid_file_name,
        admission_root=_admission_root_for_cfg(worker_cfg),
        after_init=_after_orca_worker_init,
        before_run=_before_orca_worker_run,
        after_run=_after_orca_worker_run,
        keyboard_interrupt=_log_orca_worker_interrupt,
        running_queue_id=queue_entry_id,
        running_job_factory=_make_orca_running_job,
        finalize_finished_job=replay.finalize_completed_job,
        reconcile_orphaned_running=replay.reconcile_worker_state,
        check_cancel_requests=_check_orca_cancel_requests,
        reserve_gate=_orca_reserve_gate,
    )
    return worker
