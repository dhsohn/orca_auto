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
from orca_auto.core.queue.engine import artifacts as _engine_artifacts
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.core.queue.internal_engine import (
    InternalEngineQueueModule,
    InternalEngineQueueWorkerDeps,
    InternalEngineQueueWorkerFacadeBindings,
    InternalEngineSpec,
    build_late_bound_internal_engine_queue_worker_deps,
    entry_matches_engine_identity,
    own_engine_accept_entry,
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
from orca_auto.core.statuses import TERMINAL_STATUSES
from orca_auto.core.utils import now_utc_iso
from orca_auto.flow.engines.xtb import artifacts as _queue_artifacts
from orca_auto.flow.engines.xtb import execution as _worker_execution
from orca_auto.flow.engines.xtb import terminal as _queue_terminal
from orca_auto.flow.engines.xtb import worker_terminal as _worker_terminal

from . import queue_admission as _queue_admission
from . import queue_runtime_terminal as _runtime_terminal
from .engine import ENGINE_DEFINITION
from .job_locations import (
    list_job_records_for_cfg,
    record_from_artifacts,
    resolve_job_location_for_cfg,
    runtime_roots_for_cfg,
    upsert_job_record,
)
from .queue_runtime_execution import (
    XtbQueueRuntimeWorkerExecutionCallbacks,
    build_queue_runtime_worker_execution_dependencies,
)
from .runner import (
    XtbRunResult,
    finalize_xtb_job,
    run_path_search_ts_hessian_followup,
    run_xtb_ranking_job,
    start_xtb_job,
)
from .state import (
    REPORT_MD_FILE_NAME,
    load_report_json,
    load_state,
    write_report_json,
    write_report_md_lines,
    write_state,
)
from .submission import _record_queued as _record_queued_submission

# Keep queue_runtime.subprocess available for tests/callers that patch Popen.
_SUBPROCESS_MODULE = subprocess
POLL_INTERVAL_SECONDS = 5
CANCEL_CHECK_INTERVAL_SECONDS = 1
WORKER_PID_FILE = "xtb_queue_worker.pid"
WORKER_SHUTDOWN_GRACE_SECONDS = 10.0
TERMINAL_REPAIR_SCAN_INTERVAL_SECONDS = 300.0
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
        run_path_search_ts_hessian_followup=run_path_search_ts_hessian_followup,
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
    dequeue_next=lambda root: dequeue_next(root, accept_entry_fn=own_engine_accept_entry("xtb")),
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
        mark_completed=mark_completed,
        mark_cancelled=mark_cancelled,
        mark_failed=mark_failed,
        upsert_job_record=upsert_job_record,
        notify_job_finished=notify_job_finished,
    )


def _mark_recovery_pending_state(cfg: Any, entry: Any, *, reason: str) -> None:
    _worker_execution._mark_recovery_pending_entry(cfg, entry, reason=reason)


def _terminate_process(proc: _ManagedProcess) -> bool:
    return terminate_process_group(proc)


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
    _adopt_terminal_artifacts(worker.cfg, job.queue_root, job.entry)
    _runtime_terminal.finalize_completed_job(
        _runtime_terminal_callbacks(),
        worker,
        _queue_id,
        job,
        rc,
    )


def _finalize_child_exit(worker: Any, job: _RunningJob, *, rc: int) -> None:
    _adopt_terminal_artifacts(worker.cfg, job.queue_root, job.entry)
    _queue_module.finalize_child_exit(worker, job, rc=rc)


def _sync_terminal_running_entries(worker: Any) -> None:
    monotonic_now = time.monotonic()
    full_terminal_scan = monotonic_now >= float(
        getattr(worker, "_orca_auto_terminal_repair_next_scan", 0.0) or 0.0
    )
    indexed_by_job_id: dict[str, Any] = {}
    if full_terminal_scan:
        try:
            indexed_by_job_id = {
                record.job_id: record for _root, record in list_job_records_for_cfg(worker.cfg)
            }
        except (OSError, RuntimeError, ValueError):
            indexed_by_job_id = {}
    for queue_root, entry in queue_entries_with_roots(worker.cfg):
        if not _is_xtb_queue_entry(entry):
            continue
        status = str(getattr(getattr(entry, "status", None), "value", "")).strip().lower()
        cancelled_marker_needs_repair = status == "cancelled" and (
            bool(getattr(entry, "cancel_requested", False))
            or str(getattr(entry, "error", "") or "").strip() != "cancel_requested"
        )
        if status == "running" or (
            status in TERMINAL_STATUSES
            and (full_terminal_scan or cancelled_marker_needs_repair)
            and _terminal_entry_needs_repair(
                worker.cfg,
                entry,
                status=status,
                indexed_record=indexed_by_job_id.get(str(entry.task_id)),
                index_loaded=full_terminal_scan,
            )
        ):
            _adopt_terminal_artifacts(worker.cfg, queue_root, entry)
    if full_terminal_scan:
        worker._orca_auto_terminal_repair_next_scan = (
            monotonic_now + TERMINAL_REPAIR_SCAN_INTERVAL_SECONDS
        )


def _terminal_entry_needs_repair(
    cfg: Any,
    entry: Any,
    *,
    status: str,
    indexed_record: Any | None = None,
    index_loaded: bool = False,
) -> bool:
    entry_metadata = getattr(entry, "metadata", {})
    if (
        isinstance(entry_metadata, dict)
        and str(entry_metadata.get("terminal_repair_blocked_reason") or "").strip()
    ):
        return False
    if status == "cancelled" and (
        bool(getattr(entry, "cancel_requested", False))
        or str(getattr(entry, "error", "") or "").strip() != "cancel_requested"
    ):
        return True
    try:
        job_dir = _job_dir(entry)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return False
    state = load_state(job_dir) or {}
    report = load_report_json(job_dir) or {}
    expected_reason = (
        "completed"
        if status == "completed"
        else (
            "cancel_requested"
            if status == "cancelled"
            else str(getattr(entry, "error", "") or "worker_failed").strip()
        )
    )
    if not _engine_artifacts.terminal_artifact_pair_is_consistent(
        state=state,
        report=report,
        entry=entry,
        engine="xtb",
        job_dir=job_dir,
        expected_status=status,
        expected_reason=expected_reason,
    ):
        return True
    try:
        markdown_lines = (job_dir / REPORT_MD_FILE_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return True
    if markdown_lines != _engine_artifacts.build_engine_report_markdown(report):
        return True
    entry_metadata = getattr(entry, "metadata", {})
    if not isinstance(entry_metadata, dict):
        return True
    job_type = str(entry_metadata.get("job_type") or "").strip()
    reaction_key = str(entry_metadata.get("reaction_key") or "").strip()
    selected_input = str(entry_metadata.get("selected_input_xyz") or "").strip()
    input_summary = entry_metadata.get("input_summary")
    resource_request = entry_metadata.get("resource_request")
    artifact_input = state.get("input")
    artifact_resources = state.get("resources")
    if (
        not job_type
        or not reaction_key
        or not selected_input
        or not isinstance(input_summary, dict)
        or not isinstance(resource_request, dict)
        or not isinstance(artifact_input, dict)
        or not isinstance(artifact_resources, dict)
        or (
            job_type != "ranking"
            and str(artifact_input.get("selected_xyz_path") or "") != selected_input
        )
        or (
            job_type == "ranking"
            and str(artifact_input.get("selected_xyz_path") or "")
            != _ranking_expected_selected_output(
                state,
                report,
                input_summary=input_summary,
                status=status,
                queued_selected_path=selected_input,
            )
        )
        or str(_artifact_value(state, report, "job_type", "")) != job_type
        or str(_artifact_value(state, report, "reaction_key", "")) != reaction_key
        or _artifact_value(state, report, "input_summary", None) != input_summary
        or artifact_resources.get("request") != resource_request
    ):
        return True
    indexed = indexed_record
    if not index_loaded:
        try:
            _index_root, indexed = resolve_job_location_for_cfg(cfg, str(entry.task_id))
        except (OSError, RuntimeError, ValueError):
            return True
    return bool(
        indexed is None
        or indexed.status != status
        or indexed.job_type != f"xtb_{job_type}"
        or indexed.molecule_key != reaction_key
        or indexed.selected_input_xyz != str(artifact_input.get("selected_xyz_path") or "")
        or indexed.resource_request != resource_request
        or Path(indexed.original_run_dir).expanduser().resolve() != job_dir
    )


def _artifact_value(
    state: dict[str, Any],
    report: dict[str, Any],
    key: str,
    default: Any = None,
) -> Any:
    if key in state:
        return state[key]
    state_engine_payload = state.get("engine_payload")
    if isinstance(state_engine_payload, dict) and key in state_engine_payload:
        return state_engine_payload[key]
    engine_payload = report.get("engine_payload")
    if isinstance(engine_payload, dict) and key in engine_payload:
        return engine_payload[key]
    return default


def _ranking_expected_selected_output(
    state: dict[str, Any],
    report: dict[str, Any],
    *,
    input_summary: dict[str, Any],
    status: str,
    queued_selected_path: str,
) -> str:
    if status != "completed":
        return queued_selected_path
    candidate_paths = input_summary.get("candidate_paths")
    selected_candidates = _artifact_value(state, report, "selected_candidate_paths", None)
    analysis_summary = _artifact_value(state, report, "analysis_summary", None)
    if (
        isinstance(candidate_paths, list)
        and isinstance(selected_candidates, list)
        and selected_candidates
        and isinstance(analysis_summary, dict)
    ):
        best_path = str(selected_candidates[0])
        if (
            best_path in candidate_paths
            and str(analysis_summary.get("best_candidate_path") or "") == best_path
        ):
            return best_path
    return ""


def _terminal_reason(state: dict[str, Any], report: dict[str, Any]) -> str:
    for payload in (state, report):
        status_payload = payload.get("status")
        if isinstance(status_payload, dict):
            reason = str(status_payload.get("reason") or "").strip()
            if reason:
                return reason
    return "terminal_artifact_replay"


def _adopt_terminal_artifacts(cfg: Any, queue_root: Path, entry: Any) -> bool:
    if not _is_xtb_queue_entry(entry):
        return False
    try:
        job_dir = _job_dir(entry)
        resolved_queue_root = queue_root.expanduser().resolve()
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return False
    if not job_dir.is_relative_to(resolved_queue_root):
        return False
    raw_state = load_state(job_dir) or {}
    raw_report = load_report_json(job_dir) or {}
    matched = _engine_artifacts.matching_terminal_artifacts_for_entry(
        state=raw_state,
        report=raw_report,
        entry=entry,
        engine="xtb",
        job_dir=job_dir,
    )
    durable_status = str(getattr(getattr(entry, "status", None), "value", "")).strip().lower()
    if matched is not None:
        state = matched.state
        report = matched.report
    elif durable_status in TERMINAL_STATUSES:
        exact_state = _engine_artifacts.exact_artifact_envelope_for_entry(
            raw_state,
            entry=entry,
            engine="xtb",
            job_dir=job_dir,
            require_job_dir=True,
            require_generation=True,
        )
        exact_report = _engine_artifacts.exact_artifact_envelope_for_entry(
            raw_report,
            entry=entry,
            engine="xtb",
            job_dir=job_dir,
            require_job_dir=False,
            require_generation=True,
        )
        state = exact_state
        report = {} if exact_state else exact_report
        if not state and not report:
            terminal_reason = (
                "completed"
                if durable_status == "completed"
                else (
                    "cancel_requested"
                    if durable_status == "cancelled"
                    else str(getattr(entry, "error", "") or "terminal_artifacts_unrecoverable")
                )
            )
            _queue_execution.mark_terminal_status(
                queue_root,
                entry.queue_id,
                status=durable_status,
                reason=terminal_reason,
                metadata_update={
                    "terminal_repair_blocked_reason": "terminal_artifacts_unrecoverable"
                },
                mark_completed_fn=mark_completed,
                mark_cancelled_fn=mark_cancelled,
                mark_failed_fn=mark_failed,
                expected_entry=entry,
                expected_task_id=str(entry.task_id),
            )
            return False
    else:
        return False
    record = record_from_artifacts(job_dir=job_dir, state=state, report=report)
    if (
        record is None
        or (record.status not in TERMINAL_STATUSES and durable_status not in TERMINAL_STATUSES)
        or record.job_id != str(entry.task_id)
    ):
        return False
    entry_metadata = getattr(entry, "metadata", {})
    if not isinstance(entry_metadata, dict):
        return False
    selected_input = str(entry_metadata.get("selected_input_xyz") or "").strip()
    try:
        selected_input_path = Path(selected_input).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    if not selected_input or not selected_input_path.is_relative_to(job_dir):
        return False
    try:
        artifact_selected_input_path = Path(str(record.selected_input_xyz)).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    if not artifact_selected_input_path.is_relative_to(job_dir):
        return False

    artifact_job_type = str(
        _artifact_value(state, report, "job_type", "path_search") or "path_search"
    )
    artifact_reaction_key = str(_artifact_value(state, report, "reaction_key", record.molecule_key))
    job_type = str(entry_metadata.get("job_type") or "").strip()
    reaction_key = str(entry_metadata.get("reaction_key") or "").strip()
    queue_resource_request = entry_metadata.get("resource_request")
    if not isinstance(queue_resource_request, dict):
        queue_resource_request = {}
    input_summary = entry_metadata.get("input_summary")
    if not isinstance(input_summary, dict):
        input_summary = {}
    if not job_type or not reaction_key:
        return False
    artifact_input_summary = _artifact_value(state, report, "input_summary", None)
    expected_ranking_selected = (
        _ranking_expected_selected_output(
            state,
            report,
            input_summary=input_summary,
            status=record.status,
            queued_selected_path=str(selected_input_path),
        )
        if job_type == "ranking"
        else ""
    )
    if (
        durable_status == "completed"
        and job_type == "ranking"
        and record.status == "completed"
        and not expected_ranking_selected
    ):
        _queue_execution.mark_terminal_status(
            queue_root,
            entry.queue_id,
            status="completed",
            reason="completed",
            metadata_update={
                "terminal_repair_blocked_reason": "ranking_selection_identity_unrecoverable"
            },
            mark_completed_fn=mark_completed,
            mark_cancelled_fn=mark_cancelled,
            mark_failed_fn=mark_failed,
            expected_entry=entry,
            expected_task_id=str(entry.task_id),
        )
        return False
    if durable_status not in TERMINAL_STATUSES and (
        artifact_job_type != job_type
        or artifact_reaction_key != reaction_key
        or (
            job_type == "ranking" and record.status == "completed" and not expected_ranking_selected
        )
        or (
            (job_type != "ranking" or record.status != "completed")
            and artifact_selected_input_path != selected_input_path
        )
        or artifact_input_summary != input_summary
        or dict(record.resource_request) != queue_resource_request
    ):
        return False

    def upsert_status(status: str) -> None:
        indexed_selected_input = (
            Path(expected_ranking_selected)
            if job_type == "ranking" and expected_ranking_selected and status == "completed"
            else selected_input_path
        )
        upsert_job_record(
            cfg,
            job_id=record.job_id,
            status=status,
            job_dir=job_dir,
            job_type=job_type,
            selected_input_xyz=str(indexed_selected_input),
            reaction_key=reaction_key,
            resource_request=queue_resource_request,
            resource_actual=record.resource_actual,
        )

    authoritative_payload = (
        report
        if isinstance(report.get("engine_payload"), dict) and "command" in report["engine_payload"]
        else (state or report)
    )

    def persist_terminal(status: str, reason: str) -> None:
        source_status = authoritative_payload.get("status")
        source_exit_code = (
            source_status.get("exit_code") if isinstance(source_status, dict) else None
        )
        exit_code: int | None
        if status == "completed":
            exit_code = 0
        elif status == "failed":
            exit_code = (
                source_exit_code
                if (
                    isinstance(source_status, dict)
                    and str(source_status.get("state") or "").strip().lower() == status
                    and type(source_exit_code) is int
                )
                else 1
            )
        else:
            exit_code = (
                source_exit_code
                if (
                    isinstance(source_status, dict)
                    and str(source_status.get("state") or "").strip().lower() == status
                    and type(source_exit_code) is int
                )
                else None
            )
        payloads = _engine_artifacts.canonical_terminal_artifact_payloads(
            authoritative_payload,
            job_dir=job_dir,
            status=status,
            reason=reason,
            exit_code=exit_code,
            generation=queue_entry_generation_token(entry),
            updated_at=now_utc_iso(),
        )
        for payload in (payloads.state, payloads.report):
            input_payload = payload.get("input")
            if not isinstance(input_payload, dict):
                input_payload = {}
                payload["input"] = input_payload
            canonical_selected_input = (
                Path(expected_ranking_selected)
                if job_type == "ranking" and expected_ranking_selected and status == "completed"
                else selected_input_path
            )
            input_payload.update(
                {
                    "primary_path": str(canonical_selected_input),
                    "selected_xyz_path": str(canonical_selected_input),
                }
            )
            engine_payload = payload.get("engine_payload")
            if not isinstance(engine_payload, dict):
                engine_payload = {}
                payload["engine_payload"] = engine_payload
            engine_payload.update({"job_type": job_type, "reaction_key": reaction_key})
            engine_payload["input_summary"] = dict(input_summary)
            resources = payload.get("resources")
            if not isinstance(resources, dict):
                resources = {}
                payload["resources"] = resources
            resources["request"] = dict(queue_resource_request)
        write_state(job_dir, payloads.state)
        write_report_json(job_dir, payloads.report)
        write_report_md_lines(
            job_dir,
            _engine_artifacts.build_engine_report_markdown(payloads.report),
        )
        upsert_status(status)

    terminal_reason = _terminal_reason(state, report)
    target_status = durable_status if durable_status in TERMINAL_STATUSES else record.status
    if target_status == "completed":
        target_reason = "completed"
    elif target_status == "cancelled":
        target_reason = str(getattr(entry, "error", "") or "cancel_requested").strip()
        if target_reason != "cancel_requested":
            target_reason = "cancel_requested"
    else:
        target_reason = str(getattr(entry, "error", "") or terminal_reason).strip()
        if not target_reason:
            target_reason = "worker_failed"

    terminal = _queue_execution.mark_terminal_status(
        queue_root,
        entry.queue_id,
        status=target_status,
        reason=target_reason,
        metadata_update={
            "candidate_count": int(_artifact_value(state, report, "candidate_count", 0) or 0)
        },
        mark_completed_fn=mark_completed,
        mark_cancelled_fn=mark_cancelled,
        mark_failed_fn=mark_failed,
        expected_entry=entry,
        expected_task_id=str(entry.task_id),
        before_update_fn=lambda: persist_terminal(target_status, target_reason),
    )
    if terminal is not None:
        return True
    if not get_cancel_requested(
        queue_root,
        entry.queue_id,
        expected_entry=entry,
        expected_task_id=str(entry.task_id),
    ):
        return False

    _queue_execution.mark_terminal_status(
        queue_root,
        entry.queue_id,
        status="cancelled",
        reason="cancel_requested",
        metadata_update=None,
        mark_completed_fn=mark_completed,
        mark_cancelled_fn=mark_cancelled,
        mark_failed_fn=mark_failed,
        expected_entry=entry,
        expected_task_id=str(entry.task_id),
        before_update_fn=lambda: persist_terminal("cancelled", "cancel_requested"),
    )
    return False


def _is_xtb_queue_entry(entry: Any) -> bool:
    return entry_matches_engine_identity(entry, "xtb")


def _repair_xtb_queue_publications(worker: Any) -> bool:
    from orca_auto.flow.submitters.internal_engine_submission import (
        repair_internal_engine_queue_publication,
    )

    repaired_all = True
    for queue_root, entry in queue_entries_with_roots(worker.cfg):
        if not _is_xtb_queue_entry(entry):
            continue
        try:
            repaired = repair_internal_engine_queue_publication(
                cfg=worker.cfg,
                queue_root=queue_root,
                entry=entry,
                record_queued_fn=_record_queued_submission,
                entry_matches_fn=_is_xtb_queue_entry,
            )
        except Exception:  # noqa: BLE001
            repaired = False
        if not repaired:
            repaired_all = False
    return repaired_all


def _after_xtb_worker_init(worker: Any) -> None:
    reserve_next_entry = worker._reserve_next_entry

    def reserve_next_after_publication_repair() -> tuple[str, Any | None]:
        if not _repair_xtb_queue_publications(worker):
            return "blocked", None
        return reserve_next_entry()

    worker.__dict__["_reserve_next_entry"] = reserve_next_after_publication_repair


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
        after_init=_after_xtb_worker_init,
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
