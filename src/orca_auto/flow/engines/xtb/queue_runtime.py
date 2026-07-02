from __future__ import annotations

import argparse
import subprocess
import time
from collections.abc import Callable
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
    load_xtb_config as load_config,
)
from orca_auto.core.engines.queue_worker import (
    EngineQueueWorker,
    build_engine_queue_worker,
    build_runtime_engine_queue_worker,
)
from orca_auto.core.notifications.engines import (
    notify_xtb_job_finished as notify_job_finished,
)
from orca_auto.core.notifications.engines import (
    notify_xtb_job_started as notify_job_started,
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
from orca_auto.core.queue import lifecycle as _queue_lifecycle
from orca_auto.core.queue.internal_engine import (
    InternalEngineQueueModule,
    InternalEngineQueueWorkerDeps,
    InternalEngineQueueWorkerFacadeBindings,
    InternalEngineSpec,
    build_late_bound_internal_engine_queue_worker_deps,
)
from orca_auto.core.queue.worker import (
    BackgroundRunningJob,
    config_path_for_worker,
    reconcile_orphaned_child_queue_entries,
    start_background_process,
    terminate_process_group,
)
from orca_auto.core.queue.worker import (
    ManagedProcess as _ManagedProcess,
)
from orca_auto.core.queue.worker import (
    pid_is_alive as worker_pid_is_alive,
)
from orca_auto.core.utils import now_utc_iso
from orca_auto.flow.engines.xtb import artifacts as _queue_artifacts
from orca_auto.flow.engines.xtb import execution as _worker_execution
from orca_auto.flow.engines.xtb import terminal as _queue_terminal
from orca_auto.flow.engines.xtb import worker_terminal as _worker_terminal

from . import queue_admission as _queue_admission
from . import queue_runtime_terminal as _runtime_terminal
from .engine import ENGINE_DEFINITION
from .job_locations import (
    runtime_roots_for_cfg,
    upsert_job_record,
)
from .queue_runtime_execution import (
    XtbQueueRuntimeWorkerExecutionCallbacks,
    build_queue_runtime_worker_execution_dependencies,
)
from .runner import XtbRunResult, finalize_xtb_job, run_xtb_ranking_job, start_xtb_job
from .state import (
    load_organized_ref,
    load_report_json,
    load_state,
)

# Keep queue_runtime.subprocess available for tests/callers that patch Popen.
_SUBPROCESS_MODULE = subprocess
POLL_INTERVAL_SECONDS = 5
CANCEL_CHECK_INTERVAL_SECONDS = 1
WORKER_PID_FILE = "xtb_queue_worker.pid"
WORKER_SHUTDOWN_GRACE_SECONDS = 10.0
WORKER_JOB_MODULE = _worker_execution.WORKER_JOB_MODULE
_ENGINE_SPEC = InternalEngineSpec(
    engine="xtb",
    worker_job_module=WORKER_JOB_MODULE,
    worker_pid_file_name=WORKER_PID_FILE,
)


def _queue_worker_deps() -> Any:
    return _queue_module.queue_worker_deps()


def _worker_execution_callbacks() -> XtbQueueRuntimeWorkerExecutionCallbacks:
    return XtbQueueRuntimeWorkerExecutionCallbacks(
        activate_reserved_slot=activate_reserved_slot,
        release_slot=release_slot,
        load_config=load_config,
        queue_entry_by_id=_queue_entry_by_id,
        job_dir=_job_dir,
        selected_xyz=_selected_xyz,
        job_type=_job_type,
        reaction_key=_reaction_key,
        input_summary=_input_summary,
        entry_resource_request=_queue_artifacts.entry_resource_request,
        matching_state=_worker_execution_hooks.matching_state,
        is_recovery_pending=_worker_execution.is_recovery_pending,
        write_running_state=_write_running_state,
        build_terminal_result=_build_terminal_result,
        finalize_execution_result=_finalize_execution_result,
        upsert_job_record=upsert_job_record,
        notify_job_started=notify_job_started,
        execute_queue_entry=_execute_queue_entry,
        run_xtb_ranking_job=run_xtb_ranking_job,
        start_xtb_job=start_xtb_job,
        finalize_xtb_job=finalize_xtb_job,
        terminate_process=_terminate_process,
        wait_for_cancellable_process=_queue_execution.wait_for_cancellable_process,
        sleep=time.sleep,
        now_utc_iso=now_utc_iso,
        get_cancel_requested=get_cancel_requested,
        mark_completed=mark_completed,
        mark_cancelled=mark_cancelled,
        mark_failed=mark_failed,
    )


def _worker_execution_dependencies() -> _worker_execution.WorkerExecutionDependencies:
    return build_queue_runtime_worker_execution_dependencies(
        _worker_execution_callbacks(),
        cancel_check_interval_seconds=CANCEL_CHECK_INTERVAL_SECONDS,
    )


_RunningJob = BackgroundRunningJob
_TerminalSummary = _queue_terminal.TerminalSummary


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
            mark_recovery_pending=lambda: _mark_recovery_pending_state,
            try_reserve_admission_slot=lambda: _try_reserve_admission_slot,
            start_background_job_process=lambda: _start_background_job_process,
            find_queue_entry=lambda: _queue_entry_by_id,
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
    runtime_roots_for_cfg=lambda cfg: runtime_roots_for_cfg(cfg),
    list_queue=lambda root: list_queue(root),
    dequeue_next=lambda root: dequeue_next(root),
)
_engine_runtime = _queue_module.runtime

queue_roots = _queue_module.queue_roots
queue_entries_with_roots = _queue_module.queue_entries_with_roots
dequeue_next_entry = _queue_module.dequeue_next_entry
_queue_entry_by_id = _queue_module.queue_entry_by_id
_admission_root = _queue_module.admission_root


def _pid_is_alive(pid: int) -> bool:
    return worker_pid_is_alive(pid)


_worker_execution_hooks = _worker_execution.default_worker_execution_hooks()
_job_dir = _worker_execution_hooks.job_dir
_selected_xyz = _worker_execution_hooks.selected_xyz
_job_type = _worker_execution_hooks.job_type
_reaction_key = _worker_execution_hooks.reaction_key
_input_summary = _worker_execution_hooks.input_summary

_write_execution_artifacts = _worker_terminal.write_execution_artifacts
_write_running_state = _worker_terminal.write_running_state
_build_terminal_result = _worker_terminal.build_terminal_result
build_worker_child_command = _worker_execution.build_worker_child_command


def _runtime_terminal_callbacks() -> _runtime_terminal.XtbQueueRuntimeTerminalCallbacks:
    return _runtime_terminal.XtbQueueRuntimeTerminalCallbacks(
        queue_terminal=_queue_terminal,
        queue_lifecycle=_queue_lifecycle,
        worker_execution_outcome_cls=_worker_execution.WorkerExecutionOutcome,
        job_dir=_job_dir,
        selected_xyz=_selected_xyz,
        queue_entry_by_id=_queue_entry_by_id,
        write_execution_artifacts=_write_execution_artifacts,
        load_terminal_summary_fn=_load_terminal_summary,
        ensure_terminal_queue_status_fn=_ensure_terminal_queue_status,
        print_terminal_summary_fn=_print_terminal_summary,
        live_worker_pid_slots_fn=_live_worker_pid_slots,
        pid_is_alive=_pid_is_alive,
        queue_entries_with_roots=queue_entries_with_roots,
        list_slots=list_slots,
        load_state=load_state,
        load_report_json=load_report_json,
        load_organized_ref=load_organized_ref,
        mark_completed=mark_completed,
        mark_cancelled=mark_cancelled,
        mark_failed=mark_failed,
        upsert_job_record=upsert_job_record,
        notify_job_finished=notify_job_finished,
    )


def _mark_recovery_pending_state(cfg: Any, entry: Any, *, reason: str) -> None:
    _worker_execution._mark_recovery_pending_entry(cfg, entry, reason=reason)


def _terminate_process(proc: _ManagedProcess) -> None:
    terminate_process_group(proc)


def _try_reserve_admission_slot(cfg: Any) -> str | None:
    return _queue_module.try_reserve_admission_slot(cfg)


def _print_terminal_summary(summary: _TerminalSummary) -> None:
    _queue_terminal.print_terminal_summary(summary)


def _load_terminal_summary(
    queue_root: Path, entry: Any, *, rc: int | None = None
) -> _TerminalSummary:
    return _runtime_terminal.load_terminal_summary(
        _runtime_terminal_callbacks(),
        queue_root,
        entry,
        rc=rc,
    )


def _ensure_terminal_queue_status(queue_root: Path, entry: Any, summary: _TerminalSummary) -> None:
    _runtime_terminal.ensure_terminal_queue_status(
        _runtime_terminal_callbacks(),
        queue_root,
        entry,
        summary,
    )


def _finalize_execution_result(
    cfg: Any,
    *,
    queue_root: Path,
    entry: Any,
    result: XtbRunResult,
    emit_output: bool,
    previous_state: dict[str, Any] | None = None,
    resumed: bool = False,
) -> _worker_execution.WorkerExecutionOutcome:
    return _runtime_terminal.finalize_execution_result(
        _runtime_terminal_callbacks(),
        cfg,
        queue_root=queue_root,
        entry=entry,
        result=result,
        emit_output=emit_output,
        previous_state=previous_state,
        resumed=resumed,
    )


def _execute_queue_entry(
    cfg: Any,
    *,
    queue_root: Path,
    entry: Any,
    should_cancel: Callable[[], bool] | None = None,
    register_running_job: Callable[[Any | None], None] | None = None,
    worker_job_pid: int | None = None,
    emit_output: bool = False,
) -> _worker_execution.WorkerExecutionOutcome:
    return _worker_execution.execute_queue_entry(
        cfg,
        queue_root=queue_root,
        entry=entry,
        should_cancel=should_cancel,
        register_running_job=register_running_job,
        worker_job_pid=worker_job_pid,
        emit_output=emit_output,
        dependencies=_worker_execution_dependencies(),
    )


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


read_worker_pid = _queue_module.read_worker_pid


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
    _runtime_terminal.finalize_completed_job(
        _runtime_terminal_callbacks(),
        worker,
        _queue_id,
        job,
        rc,
    )


def _finalize_child_exit(worker: Any, job: _RunningJob, *, rc: int) -> None:
    _queue_module.finalize_child_exit(worker, job, rc=rc)


def _sync_terminal_running_entries(worker: Any) -> None:
    _runtime_terminal.sync_terminal_running_entries(_runtime_terminal_callbacks(), worker)


def _live_worker_pid_slots(worker: Any) -> list[Any]:
    return _runtime_terminal.live_worker_pid_slots(_runtime_terminal_callbacks(), worker)


def _list_slots_preserving_live_worker_pids(
    worker: Any,
    admission_root: str | Path,
) -> list[Any]:
    return _runtime_terminal.list_slots_preserving_live_worker_pids(
        _runtime_terminal_callbacks(),
        worker,
        admission_root,
    )


def _reconcile_orphaned_running(worker: Any) -> None:
    _queue_module.reconcile_orphaned_running(
        worker,
        list_slots_fn=lambda admission_root: _list_slots_preserving_live_worker_pids(
            worker,
            admission_root,
        ),
    )


def _reconcile_worker_state(worker: Any) -> None:
    _sync_terminal_running_entries(worker)
    _reconcile_orphaned_running(worker)


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
        engine="xtb",
        max_concurrent=max_concurrent,
        deps=_queue_worker_deps(),
        hooks=_queue_worker_hooks(),
        worker_pid_file_name=WORKER_PID_FILE,
        admission_root=_admission_root(cfg),
        finalize_child_exit=_finalize_child_exit,
        reconcile_orphaned_running=_reconcile_orphaned_running,
        worker_builder=build_engine_queue_worker,
    )


def cmd_queue_worker(args: Any) -> int:
    return _queue_module.run_pidfile_worker_command(
        args,
        config_path_fn=_config_path_for_worker,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orca_auto.flow.engines.xtb.queue_runtime")
    parser.add_argument("--config", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    return cmd_queue_worker(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
