"""Queue worker foreground loop for queue execution under an external supervisor.

This engine worker is launched by the unified orca_auto worker service under
systemd. Each job is spawned in a dedicated child process so locking, state
management, and signal handling remain centralized.
"""

from __future__ import annotations

import copy
import logging
import time
from pathlib import Path
from typing import Any

from orca_auto.core.admission import (
    activate_reserved_slot,
    list_slots,
    reconcile_stale_slots,
    release_slot,
    reserve_slot,
    update_slot_metadata,
)
from orca_auto.core.config.schema import resolved_admission_limit
from orca_auto.core.engines.orca_execution import (
    WORKER_JOB_MODULE,
    BackgroundRunJobProcess,
    build_worker_child_command,
)
from orca_auto.core.engines.queue_worker import (
    EngineQueueWorker,
    build_engine_queue_worker,
    build_runtime_engine_queue_worker,
)
from orca_auto.core.queue.engine_execution import coerce_resource_request
from orca_auto.core.queue.internal_engine import (
    InternalEngineQueueModule,
    InternalEngineSpec,
)
from orca_auto.core.queue.lifecycle import (
    EngineQueueProcessLifecycleHooks,
    EngineQueueProcessReconcileHooks,
    EngineQueueProcessShutdownHooks,
    EngineQueueTerminalSideEffectHooks,
    cancel_running_process_job,
    finalize_process_finished_job,
    reconcile_orphaned_process_entries,
    shutdown_running_process_job,
)
from orca_auto.core.queue.lifecycle import (
    job_queue_root as _lifecycle_job_queue_root,
)
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.queue.worker import (
    EngineRunningJob as _RunningJob,
)
from orca_auto.core.queue.worker import (
    ManagedProcess as _ManagedProcess,
)
from orca_auto.core.queue.worker import (
    start_background_process,
    terminate_process_group,
)

from . import queue_worker_lifecycle as _lifecycle_helpers
from . import queue_worker_runtime as _runtime_helpers
from . import queue_worker_tracking as _tracking_helpers
from .attempt_reporting import (
    build_run_finished_notification,
    finished_notification_already_sent,
    mark_finished_notification_sent,
)
from .config import AppConfig, load_config
from .engine import ENGINE_DEFINITION
from .inp_rewriter import read_resource_request_from_input
from .input_artifacts import selected_input_artifacts
from .job_locations import (
    record_from_artifacts,
    resolve_job_metadata,
    resource_dict,
    upsert_job_record,
)
from .queue_adapter import (
    get_cancel_requested,
    list_queue,
    mark_cancelled,
    mark_completed,
    mark_failed,
    queue_entry_app_name,
    queue_entry_id,
    queue_entry_metadata,
    queue_entry_reaction_dir,
    queue_entry_task_id,
    reconcile_orphaned_running_entries,
    requeue_running_entry,
    worker_log_path,
)
from .queue_worker_deps import (
    OrcaQueueWorkerFacadeBindings,
    OrcaQueueWorkerFacadeCallbacks,
    build_late_bound_orca_runtime_facade_deps,
    build_orca_runtime_facade_deps,
)
from .state import load_organized_ref, load_report_json, load_state
from .telegram_notifier import notify_run_finished_event

# Preserve historical facade attributes for external imports and monkeypatching.
_LEGACY_COMPAT_EXPORTS = (
    EngineQueueProcessReconcileHooks,
    EngineQueueProcessShutdownHooks,
    EngineQueueTerminalSideEffectHooks,
    OrcaQueueWorkerFacadeCallbacks,
    activate_reserved_slot,
    build_orca_runtime_facade_deps,
    build_run_finished_notification,
    coerce_resource_request,
    finished_notification_already_sent,
    list_slots,
    load_config,
    load_organized_ref,
    load_report_json,
    load_state,
    mark_cancelled,
    mark_completed,
    mark_finished_notification_sent,
    notify_run_finished_event,
    queue_entry_app_name,
    queue_entry_metadata,
    reconcile_orphaned_process_entries,
    reconcile_orphaned_running_entries,
    reconcile_stale_slots,
    record_from_artifacts,
    release_slot,
    requeue_running_entry,
    resolve_job_metadata,
    resource_dict,
    selected_input_artifacts,
    shutdown_running_process_job,
    time,
    upsert_job_record,
    update_slot_metadata,
    worker_log_path,
    read_resource_request_from_input,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT = 4
POLL_INTERVAL_SECONDS = 5
WORKER_SHUTDOWN_GRACE_SECONDS = 10.0

# PID file for the daemon
WORKER_PID_FILE = "queue_worker.pid"
_ENGINE_SPEC = InternalEngineSpec(
    engine="orca",
    worker_job_module=WORKER_JOB_MODULE,
    worker_pid_file_name=WORKER_PID_FILE,
)
_ENGINE_ADMISSION = _ENGINE_SPEC.admission()


def _default_config_path() -> str:
    return ""


def config_path_for_worker(args: Any, *, default_config_path_fn: Any) -> str:
    return str(getattr(args, "config", "") or default_config_path_fn())


def _list_queue_for_runtime(root: str | Path) -> list[QueueEntry]:
    return list_queue(Path(root))


def _mark_recovery_pending_entry(*_args: Any, **_kwargs: Any) -> None:
    return None


def _runtime_facade_deps() -> Any:
    return build_late_bound_orca_runtime_facade_deps(
        OrcaQueueWorkerFacadeBindings(
            release_slot=lambda: release_slot,
            reserve_slot=lambda: _reserve_orca_worker_slot,
            start_background_process=lambda: start_background_process,
            build_worker_child_command=lambda: build_worker_child_command,
            config_path_for_worker=lambda: config_path_for_worker,
            default_config_path=lambda: _default_config_path,
            activate_reserved_slot=lambda: activate_reserved_slot,
            terminate_process=lambda: _terminate_process,
            mark_failed=lambda: mark_failed,
            handle_worker_start_error=lambda: _handle_worker_start_error,
            finalize_completed_job=lambda: _finalize_completed_job,
            finalize_child_exit=lambda: _finalize_child_exit,
            reconcile_worker_state=lambda: _reconcile_worker_state,
            list_queue=lambda: _list_queue_for_runtime,
            list_slots=lambda: list_slots,
            reconcile_stale_slots=lambda: reconcile_stale_slots,
            mark_cancelled=lambda: mark_cancelled,
            requeue_running_entry=lambda: requeue_running_entry,
            mark_recovery_pending=lambda: _mark_recovery_pending_entry,
            try_reserve_admission_slot=lambda: _try_reserve_admission_slot,
            start_background_job_process=lambda: _start_background_job_process,
            load_config=lambda: load_config,
            read_worker_pid=lambda: read_worker_pid,
            worker_class=lambda: QueueWorker,
            on_worker_process_started=lambda: _on_worker_process_started,
            shutdown_running_job=lambda: _shutdown_running_job,
            before_shutdown_all=lambda: _before_shutdown_all,
        ),
        time_module=time,
    )


_queue_module = InternalEngineQueueModule.create_from_definition(
    definition=ENGINE_DEFINITION,
    spec=_ENGINE_SPEC,
    poll_interval_seconds=POLL_INTERVAL_SECONDS,
    shutdown_grace_seconds=WORKER_SHUTDOWN_GRACE_SECONDS,
    deps=_runtime_facade_deps(),
)
_engine_runtime = _queue_module.runtime


def queue_roots(cfg: AppConfig) -> tuple[Path, ...]:
    return _queue_module.queue_roots(cfg)


def queue_entries_with_roots(cfg: AppConfig) -> list[tuple[Path, Any]]:
    return _queue_module.queue_entries_with_roots(cfg)


def _queue_worker_deps() -> Any:
    return _queue_module.queue_worker_deps()


def _admission_root_for_cfg(cfg: AppConfig) -> str:
    return _queue_module.admission_root(cfg)


def _reserve_orca_worker_slot(root: str | Path, limit: int, **kwargs: Any) -> str | None:
    slot_kwargs = dict(kwargs)
    slot_kwargs["source"] = "queue_worker"
    slot_kwargs["app_name"] = "orca_auto_orca"
    slot_kwargs["state"] = "reserved"
    return reserve_slot(
        Path(root),
        limit,
        **slot_kwargs,
    )


def _try_reserve_admission_slot(cfg: AppConfig) -> str | None:
    admission_token = _queue_module.try_reserve_admission_slot(cfg)
    if admission_token is None:
        logger.debug(
            "Queue worker admission paused: admission slots are full (admission_limit=%d)",
            int(getattr(cfg.runtime, "resolved_admission_limit", 1)),
        )
    return admission_token


def _start_background_job_process(
    *,
    config_path: str,
    queue_root: Path,
    entry: QueueEntry,
    admission_root: Any,
    admission_token: str,
) -> BackgroundRunJobProcess:
    del admission_root
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


def _tracking_callbacks() -> _tracking_helpers.OrcaQueueWorkerTrackingCallbacks:
    return _tracking_helpers.OrcaQueueWorkerTrackingCallbacks(
        build_run_finished_notification=build_run_finished_notification,
        coerce_resource_request=coerce_resource_request,
        finished_notification_already_sent=finished_notification_already_sent,
        load_organized_ref=load_organized_ref,
        load_report_json=load_report_json,
        load_state=load_state,
        mark_finished_notification_sent=mark_finished_notification_sent,
        notify_run_finished_event=notify_run_finished_event,
        queue_entry_metadata=queue_entry_metadata,
        queue_entry_reaction_dir=queue_entry_reaction_dir,
        queue_entry_task_id=queue_entry_task_id,
        read_resource_request_from_input=read_resource_request_from_input,
        record_from_artifacts=record_from_artifacts,
        resolve_job_metadata=resolve_job_metadata,
        resource_dict=resource_dict,
        selected_input_artifacts=selected_input_artifacts,
        upsert_job_record=upsert_job_record,
    )


def _get_run_id_from_state(reaction_dir: str) -> str | None:
    return _tracking_helpers.get_run_id_from_state(
        reaction_dir,
        callbacks=_tracking_callbacks(),
    )


def _upsert_running_job_record(cfg: AppConfig, entry: QueueEntry) -> None:
    _tracking_helpers.upsert_running_job_record(
        cfg,
        entry,
        callbacks=_tracking_callbacks(),
    )


def _upsert_terminal_job_record(
    cfg: AppConfig,
    reaction_dir: str,
    *,
    fallback_job_id: str | None = None,
) -> None:
    _tracking_helpers.upsert_terminal_job_record(
        cfg,
        reaction_dir,
        fallback_job_id=fallback_job_id or "",
        callbacks=_tracking_callbacks(),
    )


def _notify_terminal_job_from_state(cfg: AppConfig, reaction_dir: str) -> bool:
    return _tracking_helpers.notify_terminal_job_from_state(
        cfg,
        reaction_dir,
        callbacks=_tracking_callbacks(),
    )


def _worker_admission_limit(cfg: AppConfig, fallback_max_concurrent: int) -> int:
    raw_limit = getattr(cfg.runtime, "admission_limit", None)
    if raw_limit in (None, "", 0):
        raw_limit = fallback_max_concurrent
    return resolved_admission_limit(raw_limit, fallback_max_concurrent)


def _worker_config_with_effective_concurrency(
    cfg: AppConfig,
    configured_max: int,
) -> AppConfig:
    if getattr(cfg.runtime, "admission_limit", None) not in (None, "", 0):
        return cfg
    worker_cfg = copy.copy(cfg)
    worker_cfg.runtime = copy.copy(cfg.runtime)
    worker_cfg.runtime.max_concurrent = configured_max
    return worker_cfg


def _queue_entry_by_id(queue_root: Any, target_queue_id: str) -> QueueEntry | None:
    for entry in list_queue(Path(queue_root)):
        if queue_entry_id(entry) == target_queue_id:
            return entry
    return None


def _lifecycle_callbacks() -> _lifecycle_helpers.OrcaQueueWorkerLifecycleCallbacks:
    return _lifecycle_helpers.OrcaQueueWorkerLifecycleCallbacks(
        queue_entry_id=queue_entry_id,
        queue_entry_app_name=queue_entry_app_name,
        queue_entry_task_id=queue_entry_task_id,
        update_slot_metadata=update_slot_metadata,
        terminate_process=_terminate_process,
        mark_failed=mark_failed,
        upsert_running_job_record=_upsert_running_job_record,
        get_run_id_from_state=_get_run_id_from_state,
        get_cancel_requested=get_cancel_requested,
        mark_cancelled=mark_cancelled,
        mark_completed=mark_completed,
        upsert_terminal_job_record=_upsert_terminal_job_record,
        notify_terminal_job_from_state=_notify_terminal_job_from_state,
        find_queue_entry=_queue_entry_by_id,
        on_completed=lambda worker, job: worker._auto_organize_terminal_job(job),
        queue_roots=queue_roots,
        reconcile_stale_slots=reconcile_stale_slots,
        reconcile_orphaned_running_entries=reconcile_orphaned_running_entries,
        requeue_running_entry=requeue_running_entry,
    )


def _orca_worker_lifecycle_hooks() -> EngineQueueProcessLifecycleHooks:
    return _lifecycle_helpers.build_orca_worker_lifecycle_hooks(_lifecycle_callbacks())


def _job_queue_root(worker: Any, job: Any) -> Path:
    return _lifecycle_job_queue_root(worker, job)


def _handle_worker_start_error(
    worker: Any,
    queue_root: Path,
    entry: Any,
    admission_token: str,
    exc: OSError,
) -> None:
    queue_id = queue_entry_id(entry)
    logger.error("Failed to start job %s: %s", queue_id, exc)
    worker._mark_entry_failed_and_release(
        queue_root,
        entry,
        admission_token,
        error=str(exc),
        mark_failed_fn=mark_failed,
    )


def _on_worker_process_started(
    worker: Any,
    queue_root: Path,
    entry: Any,
    process: BackgroundRunJobProcess,
    admission_token: str,
) -> bool:
    return _ENGINE_ADMISSION.attach_started_process_metadata(
        worker=worker,
        queue_root=queue_root,
        entry=entry,
        process=process,
        admission_token=admission_token,
        hooks=_orca_worker_lifecycle_hooks(),
    )


def _finalize_finished_job(worker: Any, queue_id: str, job: _RunningJob, *, rc: int) -> None:
    finalize_process_finished_job(
        worker,
        queue_id,
        job,
        rc=rc,
        hooks=_orca_worker_lifecycle_hooks(),
    )


def _finalize_completed_job(worker: Any, queue_id: str, job: Any, rc: int) -> None:
    _finalize_finished_job(worker, queue_id, job, rc=rc)


def _finalize_child_exit(worker: Any, job: _RunningJob, *, rc: int) -> None:
    _finalize_finished_job(worker, job.queue_id, job, rc=rc)


def _reconcile_orphaned_running(worker: Any) -> None:
    _lifecycle_helpers.reconcile_orphaned_running(
        worker,
        callbacks=_lifecycle_callbacks(),
    )


def _reconcile_worker_state(worker: Any) -> None:
    _reconcile_orphaned_running(worker)


def _shutdown_running_job(worker: Any, queue_id: str, job: Any) -> None:
    _lifecycle_helpers.shutdown_running_job(
        worker,
        queue_id,
        job,
        callbacks=_lifecycle_callbacks(),
    )


def _before_shutdown_all(_worker: Any, running_count: int) -> None:
    logger.info("Shutting down %d running job(s)...", running_count)


def _queue_worker_hooks() -> Any:
    return _queue_module.queue_worker_hooks()


def _after_orca_worker_init(worker: EngineQueueWorker) -> None:
    worker.admission_limit = _worker_admission_limit(worker.cfg, worker.max_concurrent)


def _before_orca_worker_run(worker: EngineQueueWorker) -> None:
    _runtime_helpers.before_worker_run(worker)


def _after_orca_worker_run(_worker: EngineQueueWorker) -> None:
    _runtime_helpers.after_worker_run(_worker)


def _log_orca_worker_interrupt(_worker: EngineQueueWorker) -> None:
    _runtime_helpers.log_worker_interrupt(_worker)


def _make_orca_running_job(
    _worker: EngineQueueWorker,
    *,
    queue_root: Path,
    entry: Any,
    process: Any,
    admission_token: str,
) -> _RunningJob:
    return _runtime_helpers.make_running_job(
        queue_root=queue_root,
        entry=entry,
        process=process,
        admission_token=admission_token,
        queue_entry_id_fn=queue_entry_id,
        queue_entry_reaction_dir_fn=queue_entry_reaction_dir,
        queue_entry_task_id_fn=queue_entry_task_id,
        running_job_cls=_RunningJob,
    )


def _auto_organize_terminal_job(worker: EngineQueueWorker, job: _RunningJob) -> None:
    _runtime_helpers.auto_organize_terminal_job(worker, job)


def _check_orca_cancel_requests(worker: EngineQueueWorker) -> None:
    _runtime_helpers.check_cancel_requests(
        worker,
        get_cancel_requested_fn=get_cancel_requested,
        job_queue_root_fn=_job_queue_root,
        cancel_running_job_fn=_cancel_orca_running_job,
    )


def _cancel_orca_running_job(worker: EngineQueueWorker, queue_id: str, job: _RunningJob) -> None:
    cancel_running_process_job(
        worker,
        queue_id,
        job,
        hooks=_orca_worker_lifecycle_hooks(),
    )


def QueueWorker(
    cfg: AppConfig,
    config_path: str,
    *,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    auto_organize: bool = False,
) -> EngineQueueWorker:
    configured_max = max(1, int(max_concurrent))
    worker_cfg = _worker_config_with_effective_concurrency(cfg, configured_max)
    worker = build_runtime_engine_queue_worker(
        worker_cfg,
        config_path=config_path,
        default_config_path=_default_config_path,
        engine="orca",
        max_concurrent=max_concurrent,
        deps=_queue_worker_deps(),
        hooks=_queue_worker_hooks(),
        worker_pid_file_name=WORKER_PID_FILE,
        admission_root=_admission_root_for_cfg(worker_cfg),
        auto_organize=auto_organize,
        after_init=_after_orca_worker_init,
        before_run=_before_orca_worker_run,
        after_run=_after_orca_worker_run,
        keyboard_interrupt=_log_orca_worker_interrupt,
        running_queue_id=queue_entry_id,
        running_job_factory=_make_orca_running_job,
        finalize_finished_job=_finalize_finished_job,
        reconcile_orphaned_running=_reconcile_orphaned_running,
        check_cancel_requests=_check_orca_cancel_requests,
        normalize_max_concurrent=True,
        worker_builder=build_engine_queue_worker,
    )
    _runtime_helpers.install_worker_runtime_methods(
        worker,
        auto_organize_fn=_auto_organize_terminal_job,
        cancel_running_job_fn=_cancel_orca_running_job,
    )
    return worker


def read_worker_pid(allowed_root: Path) -> int | None:
    """Read the worker PID file. Returns None if not found or stale."""
    return _queue_module.read_worker_pid(allowed_root)
