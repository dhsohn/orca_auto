from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path
from typing import Any

from orca_auto.core.admission import (
    activate_reserved_slot,
    list_slots,
    reconcile_stale_slots,
    release_slot,
    reserve_slot,
)
from orca_auto.core.config.engines import (
    default_shared_config_path as default_config_path,
)
from orca_auto.core.config.engines import (
    load_crest_config as load_config,
)
from orca_auto.core.engines.queue_worker import (
    EngineQueueWorker,
    build_engine_queue_worker,
    build_runtime_engine_queue_worker,
)
from orca_auto.core.notifications.engines import (
    notify_crest_job_finished as notify_job_finished,
)
from orca_auto.core.notifications.engines import (
    notify_crest_job_started as notify_job_started,
)
from orca_auto.core.queue import (
    dequeue_next,
    get_cancel_requested,
    list_queue,
    mark_cancelled,
    mark_completed,
    mark_failed,
    requeue_running_entry,
)
from orca_auto.core.queue import (
    execution as _queue_execution,
)
from orca_auto.core.queue.internal_engine import (
    InternalEngineQueueModule,
    InternalEngineQueueWorkerDeps,
    InternalEngineQueueWorkerFacadeBindings,
    InternalEngineSpec,
    build_late_bound_internal_engine_queue_worker_deps,
    own_engine_accept_entry,
)
from orca_auto.core.queue.worker import (
    BackgroundRunningJob as _RunningJob,
)
from orca_auto.core.queue.worker import (
    config_path_for_worker,
    reconcile_orphaned_child_queue_entries,
    start_background_process,
)
from orca_auto.core.utils import now_utc_iso
from orca_auto.flow.engines.crest.execution import (
    _mark_recovery_pending_entry,
    _terminate_process,
    _write_execution_artifacts,
    _write_running_state,
    build_worker_child_command,
)

from . import queue_admission as _queue_admission
from .engine import ENGINE_DEFINITION
from .job_locations import upsert_job_record
from .queue_runtime_execution import (
    CrestQueueRuntimeWorkerExecutionCallbacks,
    build_queue_runtime_worker_execution_dependencies,
)
from .runner import finalize_crest_job, start_crest_job

# Keep queue_runtime.subprocess available for tests/callers that patch Popen.
_SUBPROCESS_MODULE = subprocess
POLL_INTERVAL_SECONDS = 5
WORKER_PID_FILE = "crest_queue_worker.pid"
WORKER_SHUTDOWN_GRACE_SECONDS = 10.0
_ENGINE_SPEC = InternalEngineSpec(
    engine="crest",
    worker_job_module="orca_auto.flow.engines.crest.execution",
    worker_pid_file_name=WORKER_PID_FILE,
)


def _queue_worker_deps() -> Any:
    return _queue_module.queue_worker_deps()


def _runtime_facade_deps() -> InternalEngineQueueWorkerDeps:
    return build_late_bound_internal_engine_queue_worker_deps(
        InternalEngineQueueWorkerFacadeBindings(
            release_slot=lambda: release_slot,
            reserve_slot=lambda: reserve_slot,
            start_background_process=lambda: start_background_process,
            build_worker_child_command=lambda: build_worker_child_command,
            config_path_for_worker=lambda: config_path_for_worker,
            default_config_path=lambda: default_config_path,
            activate_reserved_slot=lambda: activate_reserved_slot,
            terminate_process=lambda: _terminate_process,
            mark_failed=lambda: mark_failed,
            handle_worker_start_error=lambda: _handle_worker_start_error,
            finalize_completed_job=lambda: _finalize_completed_job,
            finalize_child_exit=lambda: _finalize_child_exit,
            reconcile_worker_state=lambda: _reconcile_worker_state,
            list_queue=lambda: list_queue,
            list_slots=lambda: list_slots,
            reconcile_stale_slots=lambda: reconcile_stale_slots,
            reconcile_orphaned_child_queue_entries=lambda: reconcile_orphaned_child_queue_entries,
            mark_cancelled=lambda: mark_cancelled,
            requeue_running_entry=lambda: requeue_running_entry,
            mark_recovery_pending=lambda: _mark_recovery_pending_entry,
            try_reserve_admission_slot=lambda: _try_reserve_admission_slot,
            start_background_job_process=lambda: _start_background_job_process,
            find_queue_entry=lambda: _find_queue_entry,
            load_config=lambda: load_config,
            read_worker_pid=lambda: read_worker_pid,
            worker_class=lambda: QueueWorker,
        ),
        time_module=time,
    )


_queue_module = InternalEngineQueueModule.create_from_definition(
    definition=ENGINE_DEFINITION,
    spec=_ENGINE_SPEC,
    poll_interval_seconds=POLL_INTERVAL_SECONDS,
    shutdown_grace_seconds=WORKER_SHUTDOWN_GRACE_SECONDS,
    deps=_runtime_facade_deps(),
    list_queue=lambda root: list_queue(root),
    dequeue_next=lambda root: dequeue_next(root, accept_entry_fn=own_engine_accept_entry("crest")),
)
_engine_runtime = _queue_module.runtime

queue_roots = _queue_module.queue_roots
queue_entries_with_roots = _queue_module.queue_entries_with_roots
_find_queue_entry = _queue_module.queue_entry_by_id
dequeue_next_entry = _queue_module.dequeue_next_entry
_admission_root_for_cfg = _queue_module.admission_root


def _try_reserve_admission_slot(cfg: Any) -> str | None:
    return _queue_module.try_reserve_admission_slot(cfg)


def _worker_execution_callbacks() -> CrestQueueRuntimeWorkerExecutionCallbacks:
    return CrestQueueRuntimeWorkerExecutionCallbacks(
        terminate_process=_terminate_process,
        wait_for_cancellable_process=_queue_execution.wait_for_cancellable_process,
        sleep=time.sleep,
        now_utc_iso=now_utc_iso,
        get_cancel_requested=get_cancel_requested,
        mark_completed=mark_completed,
        mark_cancelled=mark_cancelled,
        mark_failed=mark_failed,
        start_crest_job=start_crest_job,
        finalize_crest_job=finalize_crest_job,
        write_running_state=_write_running_state,
        write_execution_artifacts=_write_execution_artifacts,
        upsert_job_record=upsert_job_record,
        notify_job_started=notify_job_started,
        notify_job_finished=notify_job_finished,
    )


def _worker_dependencies() -> Any:
    return build_queue_runtime_worker_execution_dependencies(
        _worker_execution_callbacks(),
        cancel_check_interval_seconds=1,
    )


read_worker_pid = _queue_module.read_worker_pid


def _start_background_job_process(
    *,
    config_path: str,
    queue_root: Path,
    entry: Any,
    admission_root: str | Path,
    admission_token: str,
) -> Any:
    return _queue_module.start_background_job_process(
        config_path=config_path,
        queue_root=queue_root,
        entry=entry,
        admission_root=admission_root,
        admission_token=admission_token,
    )


def _config_path_for_worker(args: Any) -> str:
    return _queue_module.config_path_for_worker(args)


def _reconcile_orphaned_running(worker: Any) -> None:
    _queue_module.reconcile_orphaned_running(worker)


def _reconcile_worker_state(worker: Any) -> None:
    _reconcile_orphaned_running(worker)


def _handle_worker_start_error(
    worker: Any,
    queue_root: Path,
    entry: Any,
    admission_token: str,
    exc: OSError,
) -> None:
    _queue_admission.mark_worker_start_error(
        queue_root=queue_root,
        entry=entry,
        admission_token=admission_token,
        exc=exc,
        mark_entry_failed_and_release_fn=worker._mark_entry_failed_and_release,
        mark_failed_fn=mark_failed,
    )


def _finalize_completed_job(worker: Any, _queue_id: str, job: Any, rc: int) -> None:
    _finalize_child_exit(worker, job, rc=rc)


def _finalize_child_exit(worker: Any, job: _RunningJob, *, rc: int) -> None:
    _queue_module.finalize_child_exit(worker, job, rc=rc)


def _queue_worker_hooks() -> Any:
    return _queue_module.queue_worker_hooks()


def QueueWorker(
    cfg: Any,
    config_path: str | None = None,
    *,
    max_concurrent: int | None = None,
) -> EngineQueueWorker:
    return build_runtime_engine_queue_worker(
        cfg,
        config_path=config_path,
        default_config_path=default_config_path,
        engine="crest",
        max_concurrent=max_concurrent,
        deps=_queue_worker_deps(),
        hooks=_queue_worker_hooks(),
        worker_pid_file_name=WORKER_PID_FILE,
        admission_root=_admission_root_for_cfg(cfg),
        finalize_child_exit=_finalize_child_exit,
        reconcile_orphaned_running=_reconcile_orphaned_running,
        normalize_max_concurrent=True,
        worker_builder=build_engine_queue_worker,
    )


def cmd_queue_worker(args: Any) -> int:
    return _queue_module.run_pidfile_worker_command(
        args,
        config_path_fn=_config_path_for_worker,
        config_path_keyword=False,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orca_auto.flow.engines.crest.queue_runtime")
    parser.add_argument("--config", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    return cmd_queue_worker(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
