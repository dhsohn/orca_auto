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
from orca_auto.core.engines import entry_matches_engine_identity
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
    execution as _queue_execution,
)
from orca_auto.core.queue import (
    get_cancel_requested,
    list_queue,
    mark_cancelled,
    mark_completed,
    mark_failed,
    requeue_running_entry,
)
from orca_auto.core.queue import lifecycle as _queue_lifecycle
from orca_auto.core.queue.engine import artifacts as _engine_artifacts
from orca_auto.core.queue.generation import queue_entry_generation_token
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
from orca_auto.flow.engines import queue_runtime_common as _common
from orca_auto.flow.engines.xtb import artifacts as _queue_artifacts
from orca_auto.flow.engines.xtb import execution as _worker_execution
from orca_auto.flow.engines.xtb import terminal as _queue_terminal
from orca_auto.flow.engines.xtb import worker_terminal as _worker_terminal

from . import queue_runtime_terminal as _runtime_terminal
from .engine import ENGINE_DEFINITION
from .job_locations import (
    list_job_records_for_cfg,
    record_from_artifacts,
    resolve_job_location_for_cfg,
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
    load_state,
    write_state,
)
from .submission import _record_queued as _record_queued_submission

# Keep queue_runtime.subprocess available for tests/callers that patch Popen.
_SUBPROCESS_MODULE = subprocess
POLL_INTERVAL_SECONDS = 5
CANCEL_CHECK_INTERVAL_SECONDS = 1
WORKER_SHUTDOWN_GRACE_SECONDS = 10.0
TERMINAL_REPAIR_SCAN_INTERVAL_SECONDS = 300.0
_ENGINE_RUNTIME = ENGINE_DEFINITION.build_queue_runtime()


def _queue_worker_deps() -> Any:
    return _ENGINE_RUNTIME.child_worker_deps(
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
        time_module=time,
        release_slot_fn=lambda root, token: release_slot(root, token),
        start_background_job_process_fn=lambda **kwargs: _start_background_job_process(**kwargs),
        try_reserve_admission_slot_fn=lambda cfg: _try_reserve_admission_slot(cfg),
    )


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

queue_roots = _ENGINE_RUNTIME.queue_roots
dequeue_next_entry = _ENGINE_RUNTIME.dequeue_next_entry
_queue_entry_by_id = _ENGINE_RUNTIME.queue_entry_by_id
_admission_root = _ENGINE_RUNTIME.admission_root


def queue_entries_with_roots(cfg: Any) -> list[tuple[Path, Any]]:
    return _ENGINE_RUNTIME.queue_entries_with_roots(cfg, list_queue_fn=list_queue)


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
    return _ENGINE_RUNTIME.reserve_admission_slot(
        cfg,
        engine="xtb",
        reserve_slot_fn=reserve_slot,
    )


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
    return _ENGINE_RUNTIME.start_child_process(
        config_path=config_path,
        queue_root=queue_root,
        entry=entry,
        admission_root=admission_root,
        admission_token=admission_token,
        start_background_process_fn=start_background_process,
        build_worker_child_command_fn=build_worker_child_command,
        include_admission_root=False,
    )


def _config_path_for_worker(args: Any) -> str:
    return config_path_for_worker(args, default_config_path_fn=default_config_path)


read_worker_pid = _ENGINE_RUNTIME.read_worker_pid


def _handle_worker_start_error(
    worker: Any,
    queue_root: Path,
    entry: Any,
    admission_token: str,
    exc: OSError,
) -> None:
    _common.handle_worker_start_error(
        worker,
        queue_root,
        entry,
        admission_token,
        exc,
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
    _ENGINE_RUNTIME.finalize_child_exit(
        worker.cfg,
        job,
        rc=rc,
        shutdown_requested=worker._shutdown_requested,
        admission_root=getattr(worker, "admission_root", None),
        find_queue_entry_fn=_queue_entry_by_id,
        mark_cancelled_fn=mark_cancelled,
        requeue_running_entry_fn=requeue_running_entry,
        mark_failed_fn=mark_failed,
        mark_recovery_pending_fn=_mark_recovery_pending_state,
        release_admission_slot_fn=worker._release_admission_slot,
    )


def _sync_terminal_running_entries(worker: Any) -> None:
    _common.sync_terminal_running_entries(
        worker,
        is_engine_entry_fn=_is_xtb_queue_entry,
        queue_entries_with_roots_fn=queue_entries_with_roots,
        list_job_records_for_cfg_fn=list_job_records_for_cfg,
        terminal_entry_needs_repair_fn=_terminal_entry_needs_repair,
        adopt_terminal_artifacts_fn=_adopt_terminal_artifacts,
        scan_interval_seconds=TERMINAL_REPAIR_SCAN_INTERVAL_SECONDS,
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
    expected_reason = (
        "completed"
        if status == "completed"
        else (
            "cancel_requested"
            if status == "cancelled"
            else str(getattr(entry, "error", "") or "worker_failed").strip()
        )
    )
    if not _engine_artifacts.terminal_state_is_consistent(
        state=state,
        entry=entry,
        engine="xtb",
        job_dir=job_dir,
        expected_status=status,
        expected_reason=expected_reason,
    ):
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
                input_summary=input_summary,
                status=status,
                queued_selected_path=selected_input,
            )
        )
        or str(_artifact_value(state, "job_type", "")) != job_type
        or str(_artifact_value(state, "reaction_key", "")) != reaction_key
        or _artifact_value(state, "input_summary", None) != input_summary
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


_artifact_value = _common.artifact_value


def _ranking_expected_selected_output(
    state: dict[str, Any],
    *,
    input_summary: dict[str, Any],
    status: str,
    queued_selected_path: str,
) -> str:
    if status != "completed":
        return queued_selected_path
    candidate_paths = input_summary.get("candidate_paths")
    selected_candidates = _artifact_value(state, "selected_candidate_paths", None)
    analysis_summary = _artifact_value(state, "analysis_summary", None)
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
    durable_status = _common.durable_entry_status(entry)
    matched = _common.matched_terminal_state_for_adoption(
        queue_root,
        entry,
        raw_state=raw_state,
        engine="xtb",
        job_dir=job_dir,
        mark_completed_fn=mark_completed,
        mark_cancelled_fn=mark_cancelled,
        mark_failed_fn=mark_failed,
    )
    if matched is None:
        return False
    state = matched
    record = record_from_artifacts(job_dir=job_dir, state=state)
    if (
        record is None
        or (record.status not in TERMINAL_STATUSES and durable_status not in TERMINAL_STATUSES)
        or record.job_id != str(entry.task_id)
    ):
        return False
    entry_metadata = getattr(entry, "metadata", {})
    if not isinstance(entry_metadata, dict):
        return False
    resolved_inputs = _common.resolve_adopted_selected_input(entry_metadata, record, job_dir)
    if resolved_inputs is None:
        return False
    selected_input_path, artifact_selected_input_path = resolved_inputs

    artifact_job_type = str(_artifact_value(state, "job_type", "path_search") or "path_search")
    artifact_reaction_key = str(_artifact_value(state, "reaction_key", record.molecule_key))
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
    artifact_input_summary = _artifact_value(state, "input_summary", None)
    expected_ranking_selected = (
        _ranking_expected_selected_output(
            state,
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

    authoritative_payload = state

    def persist_terminal(status: str, reason: str) -> None:
        payload = _engine_artifacts.canonical_terminal_state_payload(
            authoritative_payload,
            job_dir=job_dir,
            status=status,
            reason=reason,
            exit_code=_common.resolve_terminal_exit_code(authoritative_payload, status),
            generation=queue_entry_generation_token(entry),
            updated_at=now_utc_iso(),
        )
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
        write_state(job_dir, payload)
        upsert_status(status)

    target_status, target_reason = _common.resolve_terminal_target(
        entry,
        state,
        durable_status=durable_status,
        record_status=record.status,
    )
    return _common.adopt_mark_terminal(
        queue_root,
        entry,
        target_status=target_status,
        target_reason=target_reason,
        metadata_update={"candidate_count": int(_artifact_value(state, "candidate_count", 0) or 0)},
        persist_terminal_fn=persist_terminal,
        get_cancel_requested_fn=get_cancel_requested,
        mark_completed_fn=mark_completed,
        mark_cancelled_fn=mark_cancelled,
        mark_failed_fn=mark_failed,
    )


def _is_xtb_queue_entry(entry: Any) -> bool:
    return entry_matches_engine_identity(entry, "xtb")


def _repair_xtb_queue_publications(worker: Any) -> bool:
    return _common.repair_engine_queue_publications(
        worker,
        queue_entries_with_roots_fn=queue_entries_with_roots,
        is_engine_entry_fn=_is_xtb_queue_entry,
        record_queued_fn=_record_queued_submission,
    )


def _after_xtb_worker_init(worker: Any) -> None:
    _common.install_publication_repair_gate(worker, repair_fn=_repair_xtb_queue_publications)


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
    _ENGINE_RUNTIME.reconcile_orphaned_running(
        worker.cfg,
        admission_root=worker.admission_root,
        list_slots_fn=lambda admission_root: _list_slots_preserving_live_worker_pids(
            worker,
            admission_root,
        ),
        reconcile_stale_slots_fn=reconcile_stale_slots,
        reconcile_orphaned_child_queue_entries_fn=reconcile_orphaned_child_queue_entries,
        mark_cancelled_fn=mark_cancelled,
        requeue_running_entry_fn=requeue_running_entry,
        mark_recovery_pending_fn=_mark_recovery_pending_state,
        list_queue_fn=list_queue,
    )


def _reconcile_worker_state(worker: Any) -> None:
    _sync_terminal_running_entries(worker)
    _reconcile_orphaned_running(worker)


def _queue_worker_hooks() -> Any:
    return _ENGINE_RUNTIME.child_worker_hooks(
        engine="xtb",
        handle_worker_start_error_fn=_handle_worker_start_error,
        finalize_completed_job_fn=_finalize_completed_job,
        finalize_child_exit_fn=_finalize_child_exit,
        reconcile_worker_state_fn=_reconcile_worker_state,
        activate_reserved_slot_fn=lambda *args, **kwargs: activate_reserved_slot(*args, **kwargs),
        terminate_process_fn=lambda process: _terminate_process(process),
        mark_failed_fn=lambda *args, **kwargs: mark_failed(*args, **kwargs),
        shutdown_grace_seconds=WORKER_SHUTDOWN_GRACE_SECONDS,
        sleep_fn=lambda seconds: time.sleep(seconds),
    )


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
        worker_pid_file_name=ENGINE_DEFINITION.queue_functions.worker_pid_file_name,
        admission_root=_admission_root(cfg),
        after_init=_after_xtb_worker_init,
        finalize_child_exit=_finalize_child_exit,
        reconcile_orphaned_running=_reconcile_orphaned_running,
        worker_builder=build_engine_queue_worker,
    )


def cmd_queue_worker(args: Any) -> int:
    return _ENGINE_RUNTIME.run_pidfile_worker_command(
        args,
        config_path_fn=_config_path_for_worker,
        load_config_fn=load_config,
        read_worker_pid_fn=read_worker_pid,
        worker_factory=lambda cfg, config_path, **kwargs: QueueWorker(
            cfg,
            config_path=config_path,
            **kwargs,
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orca_auto.flow.engines.xtb.queue_runtime")
    parser.add_argument("--config", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    return cmd_queue_worker(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
