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
from orca_auto.core.engines import entry_matches_engine_identity
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
    execution as _queue_execution,
)
from orca_auto.core.queue import (
    get_cancel_requested,
    mark_cancelled,
    mark_completed,
    mark_failed,
    requeue_running_entry,
)
from orca_auto.core.queue.engine import artifacts as _engine_artifacts
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.core.queue.worker import (
    BackgroundRunningJob as _RunningJob,
)
from orca_auto.core.queue.worker import (
    config_path_for_worker,
    reconcile_orphaned_child_queue_entries,
    start_background_process,
)
from orca_auto.core.statuses import TERMINAL_STATUSES
from orca_auto.core.utils import now_utc_iso
from orca_auto.flow.engines import queue_runtime_common as _common
from orca_auto.flow.engines.crest.execution import (
    _mark_recovery_pending_entry,
    _terminate_process,
    _write_execution_artifacts,
    _write_running_state,
    build_worker_child_command,
)

from .engine import ENGINE_DEFINITION
from .job_locations import (
    list_job_records_for_cfg,
    record_from_artifacts,
    resolve_job_location_for_cfg,
    upsert_job_record,
)
from .queue_runtime_execution import (
    CrestQueueRuntimeWorkerExecutionCallbacks,
    build_queue_runtime_worker_execution_dependencies,
)
from .runner import finalize_crest_job, start_crest_job
from .state import (
    load_state,
    write_state,
)
from .submission import _record_queued as _record_queued_submission

# Keep queue_runtime.subprocess available for tests/callers that patch Popen.
_SUBPROCESS_MODULE = subprocess
POLL_INTERVAL_SECONDS = 5
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


queue_roots = _ENGINE_RUNTIME.queue_roots
queue_entries_with_roots = _ENGINE_RUNTIME.queue_entries_with_roots
_find_queue_entry = _ENGINE_RUNTIME.queue_entry_by_id
dequeue_next_entry = _ENGINE_RUNTIME.dequeue_next_entry
_admission_root_for_cfg = _ENGINE_RUNTIME.admission_root


def _try_reserve_admission_slot(cfg: Any) -> str | None:
    return _ENGINE_RUNTIME.reserve_admission_slot(
        cfg,
        engine="crest",
        reserve_slot_fn=reserve_slot,
    )


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


read_worker_pid = _ENGINE_RUNTIME.read_worker_pid


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


def _reconcile_orphaned_running(worker: Any) -> None:
    _ENGINE_RUNTIME.reconcile_orphaned_running(
        worker.cfg,
        admission_root=worker.admission_root,
        list_slots_fn=list_slots,
        reconcile_stale_slots_fn=reconcile_stale_slots,
        reconcile_orphaned_child_queue_entries_fn=reconcile_orphaned_child_queue_entries,
        mark_cancelled_fn=mark_cancelled,
        requeue_running_entry_fn=requeue_running_entry,
        mark_recovery_pending_fn=_mark_recovery_pending_entry,
    )


_artifact_value = _common.artifact_value


def _adopt_terminal_artifacts(cfg: Any, queue_root: Path, entry: Any) -> bool:
    if not _is_crest_queue_entry(entry):
        return False
    job_dir_text = str(getattr(entry, "metadata", {}).get("job_dir") or "").strip()
    if not job_dir_text:
        return False
    try:
        job_dir = Path(job_dir_text).expanduser().resolve()
        resolved_queue_root = queue_root.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    if not job_dir.is_relative_to(resolved_queue_root):
        return False
    raw_state = load_state(job_dir) or {}
    durable_status = _common.durable_entry_status(entry)
    matched = _common.matched_terminal_state_for_adoption(
        queue_root,
        entry,
        raw_state=raw_state,
        engine="crest",
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

    artifact_mode = str(_artifact_value(state, "mode", "standard") or "standard")
    artifact_molecule_key = str(_artifact_value(state, "molecule_key", record.molecule_key))
    mode = str(entry_metadata.get("mode") or "").strip()
    molecule_key = str(entry_metadata.get("molecule_key") or "").strip()
    queue_resource_request = entry_metadata.get("resource_request")
    if not isinstance(queue_resource_request, dict):
        queue_resource_request = {}
    if not mode or not molecule_key:
        return False
    if durable_status not in TERMINAL_STATUSES and (
        artifact_mode != mode
        or artifact_molecule_key != molecule_key
        or artifact_selected_input_path != selected_input_path
        or dict(record.resource_request) != queue_resource_request
    ):
        return False

    def upsert_status(status: str) -> None:
        upsert_job_record(
            cfg,
            job_id=record.job_id,
            status=status,
            job_dir=job_dir,
            mode=mode,
            selected_input_xyz=str(selected_input_path),
            molecule_key=molecule_key,
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
        input_payload.update(
            {
                "primary_path": str(selected_input_path),
                "selected_xyz_path": str(selected_input_path),
            }
        )
        engine_payload = payload.get("engine_payload")
        if not isinstance(engine_payload, dict):
            engine_payload = {}
            payload["engine_payload"] = engine_payload
        engine_payload.update({"mode": mode, "molecule_key": molecule_key})
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
        metadata_update={
            "retained_conformer_count": int(
                _artifact_value(state, "retained_conformer_count", 0) or 0
            ),
        },
        persist_terminal_fn=persist_terminal,
        get_cancel_requested_fn=get_cancel_requested,
        mark_completed_fn=mark_completed,
        mark_cancelled_fn=mark_cancelled,
        mark_failed_fn=mark_failed,
    )


def _sync_terminal_running_entries(worker: Any) -> None:
    _common.sync_terminal_running_entries(
        worker,
        is_engine_entry_fn=_is_crest_queue_entry,
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
    job_dir_text = str(getattr(entry, "metadata", {}).get("job_dir") or "").strip()
    if not job_dir_text:
        return False
    try:
        job_dir = Path(job_dir_text).expanduser().resolve()
    except (OSError, RuntimeError):
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
        engine="crest",
        job_dir=job_dir,
        expected_status=status,
        expected_reason=expected_reason,
    ):
        return True
    entry_metadata = getattr(entry, "metadata", {})
    if not isinstance(entry_metadata, dict):
        return True
    mode = str(entry_metadata.get("mode") or "").strip()
    molecule_key = str(entry_metadata.get("molecule_key") or "").strip()
    selected_input = str(entry_metadata.get("selected_input_xyz") or "").strip()
    resource_request = entry_metadata.get("resource_request")
    artifact_input = state.get("input")
    artifact_resources = state.get("resources")
    if (
        not mode
        or not molecule_key
        or not selected_input
        or not isinstance(resource_request, dict)
        or not isinstance(artifact_input, dict)
        or not isinstance(artifact_resources, dict)
        or str(artifact_input.get("selected_xyz_path") or "") != selected_input
        or str(_artifact_value(state, "mode", "")) != mode
        or str(_artifact_value(state, "molecule_key", "")) != molecule_key
        or artifact_resources.get("request") != resource_request
    ):
        return True
    indexed = indexed_record
    if not index_loaded:
        try:
            _index_root, indexed = resolve_job_location_for_cfg(cfg, str(entry.task_id))
        except (OSError, RuntimeError, ValueError):
            return True
    expected_job_type = (
        "crest_nci_conformer_search" if mode == "nci" else "crest_standard_conformer_search"
    )
    return bool(
        indexed is None
        or indexed.status != status
        or indexed.job_type != expected_job_type
        or indexed.molecule_key != molecule_key
        or indexed.selected_input_xyz != selected_input
        or indexed.resource_request != resource_request
        or Path(indexed.original_run_dir).expanduser().resolve() != job_dir
    )


def _is_crest_queue_entry(entry: Any) -> bool:
    return entry_matches_engine_identity(entry, "crest")


def _repair_crest_queue_publications(worker: Any) -> bool:
    return _common.repair_engine_queue_publications(
        worker,
        engine="CREST",
        queue_entries_with_roots_fn=queue_entries_with_roots,
        is_engine_entry_fn=_is_crest_queue_entry,
        record_queued_fn=_record_queued_submission,
    )


def _after_crest_worker_init(worker: Any) -> None:
    _common.install_publication_repair_gate(
        worker,
        engine="CREST",
        repair_fn=_repair_crest_queue_publications,
    )


def _reconcile_worker_state(worker: Any) -> None:
    _sync_terminal_running_entries(worker)
    _reconcile_orphaned_running(worker)


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
    _finalize_child_exit(worker, job, rc=rc)


def _finalize_child_exit(worker: Any, job: _RunningJob, *, rc: int) -> None:
    _adopt_terminal_artifacts(worker.cfg, job.queue_root, job.entry)
    _ENGINE_RUNTIME.finalize_child_exit(
        worker.cfg,
        job,
        rc=rc,
        shutdown_requested=worker._shutdown_requested,
        admission_root=getattr(worker, "admission_root", None),
        find_queue_entry_fn=_find_queue_entry,
        mark_cancelled_fn=mark_cancelled,
        requeue_running_entry_fn=requeue_running_entry,
        mark_failed_fn=mark_failed,
        mark_recovery_pending_fn=_mark_recovery_pending_entry,
        release_admission_slot_fn=worker._release_admission_slot,
    )


def _queue_worker_hooks() -> Any:
    return _ENGINE_RUNTIME.child_worker_hooks(
        engine="crest",
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
        engine="crest",
        max_concurrent=max_concurrent,
        deps=_queue_worker_deps(),
        hooks=_queue_worker_hooks(),
        worker_pid_file_name=ENGINE_DEFINITION.queue_functions.worker_pid_file_name,
        admission_root=_admission_root_for_cfg(cfg),
        after_init=_after_crest_worker_init,
        finalize_child_exit=_finalize_child_exit,
        reconcile_orphaned_running=_reconcile_orphaned_running,
        normalize_max_concurrent=True,
        worker_builder=build_engine_queue_worker,
    )


def cmd_queue_worker(args: Any) -> int:
    return _ENGINE_RUNTIME.run_pidfile_worker_command(
        args,
        config_path_fn=_config_path_for_worker,
        load_config_fn=load_config,
        read_worker_pid_fn=read_worker_pid,
        worker_factory=QueueWorker,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orca_auto.flow.engines.crest.queue_runtime")
    parser.add_argument("--config", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    return cmd_queue_worker(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
