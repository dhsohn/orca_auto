from __future__ import annotations

import argparse
import dataclasses
import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.admission import activate_reserved_slot, release_slot
from orca_auto.core.config.engines import load_crest_config as load_config
from orca_auto.core.engines import entry_matches_engine_identity
from orca_auto.core.engines.worker_child import (
    WORKER_CHILD_MODULE,
    build_worker_child_command_for_engine,
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
    list_queue,
    mark_cancelled,
    mark_completed,
    mark_failed,
    requeue_running_entry,
)
from orca_auto.core.queue.child.execution import install_shutdown_request_handlers
from orca_auto.core.queue.engine import execution as _engine_execution
from orca_auto.core.queue.engine.child import (
    WorkerChildRunSpec,
    run_engine_worker_child_job,
)
from orca_auto.core.queue.engine.input_snapshot import (
    read_stable_regular_file,
    verify_input_snapshots,
)
from orca_auto.core.queue.lifecycle import entry_status_is_running
from orca_auto.core.queue.worker import execution_dependencies as _worker_dependencies
from orca_auto.core.queue.worker import (
    install_shutdown_signal_handlers,
    resolve_admission_root,
    terminate_process_group,
)
from orca_auto.core.utils import now_utc_iso
from orca_auto.flow.engines.crest import artifacts as _queue_artifacts
from orca_auto.flow.engines.crest.job_locations import runtime_roots_for_cfg, upsert_job_record
from orca_auto.flow.engines.crest.runner import CrestRunResult, finalize_crest_job, start_crest_job
from orca_auto.flow.engines.crest.state import mark_recovery_pending
from orca_auto.flow.engines.crest.terminal import (
    WorkerExecutionOutcome,
)
from orca_auto.flow.engines.crest.terminal import (
    finalize_processed_entry as _terminal_finalize_processed_entry,
)
from orca_auto.flow.engines.crest.terminal import (
    mark_job_running as _terminal_mark_job_running,
)
from orca_auto.flow.engines.crest.terminal import (
    mark_queue_terminal as _terminal_mark_queue_terminal,
)
from orca_auto.flow.engines.crest.terminal import (
    sync_job_tracking as _terminal_sync_job_tracking,
)
from orca_auto.flow.engines.crest.terminal import (
    write_execution_artifacts as _terminal_write_execution_artifacts,
)
from orca_auto.flow.engines.crest.terminal import (
    write_running_state as _terminal_write_running_state,
)
from orca_auto.flow.engines.crest.worker_context import (
    ExecutionContext,
)
from orca_auto.flow.engines.crest.worker_context import (
    mode as _mode,
)
from orca_auto.flow.engines.crest.worker_context import (
    molecule_key as _molecule_key,
)

CANCEL_CHECK_INTERVAL_SECONDS = 1
WORKER_JOB_MODULE = WORKER_CHILD_MODULE
is_recovery_pending = _queue_artifacts.is_recovery_pending
load_state = _queue_artifacts.load_state
state_matches_job = _queue_artifacts.state_matches_job
write_state = _queue_artifacts.write_state
build_worker_child_command = build_worker_child_command_for_engine("crest")


class WorkerShutdownRequested(RuntimeError):
    def __init__(self, context: Any):
        super().__init__("worker_shutdown")
        self.context = context


_WORKER_CHILD_RUN_SPEC = WorkerChildRunSpec(
    shutdown_exception_type=WorkerShutdownRequested,
    entry_ready_fn=lambda entry: (
        entry_status_is_running(entry) and entry_matches_engine_identity(entry, "crest")
    ),
)


WorkerConfigDependencies = _worker_dependencies.WorkerConfigDependencies
WorkerAdmissionDependencies = _worker_dependencies.WorkerAdmissionDependencies
WorkerTimingDependencies = _engine_execution.EngineWorkerTimingDependencies
WorkerQueueDependencies = _engine_execution.EngineWorkerQueueDependencies


@dataclass(frozen=True)
class WorkerRunnerDependencies(_engine_execution.EngineWorkerProcessDependencies):
    start_crest_job: Callable[..., Any]
    finalize_crest_job: Callable[..., CrestRunResult]


@dataclass(frozen=True)
class WorkerContextDependencies:
    job_dir: Callable[[Any], Path]
    selected_xyz: Callable[[Any], Path]
    molecule_key: Callable[[Any, Path, Path], str]
    mode: Callable[[Any], str]
    entry_resource_request: Callable[[Any, Any], dict[str, int]]


@dataclass(frozen=True)
class WorkerArtifactDependencies:
    write_running_state: Callable[[Any, Any], None]
    write_execution_artifacts: Callable[[Any, CrestRunResult], None]


@dataclass(frozen=True)
class WorkerTrackingDependencies:
    upsert_job_record: Callable[..., Any]
    notify_job_started: Callable[..., bool]
    notify_job_finished: Callable[..., bool]


@dataclass(frozen=True)
class WorkerExecutionDependencies:
    timing: WorkerTimingDependencies
    queue: WorkerQueueDependencies
    runner: WorkerRunnerDependencies
    artifacts: WorkerArtifactDependencies
    tracking: WorkerTrackingDependencies
    config: WorkerConfigDependencies = field(default_factory=lambda: _default_config_dependencies())
    admission: WorkerAdmissionDependencies = field(
        default_factory=lambda: _default_admission_dependencies()
    )
    context: WorkerContextDependencies = field(
        default_factory=lambda: _default_context_dependencies()
    )
    execute_queue_entry: Callable[..., Any] | None = None


def build_worker_execution_dependencies_from_groups(
    *,
    timing: WorkerTimingDependencies,
    queue: WorkerQueueDependencies,
    runner: WorkerRunnerDependencies,
    artifacts: WorkerArtifactDependencies,
    tracking: WorkerTrackingDependencies,
    config: WorkerConfigDependencies | None = None,
    admission: WorkerAdmissionDependencies | None = None,
    context: WorkerContextDependencies | None = None,
    execute_queue_entry_fn: Callable[..., Any] | None = None,
) -> WorkerExecutionDependencies:
    return _worker_dependencies.build_worker_execution_dependencies_from_groups(
        WorkerExecutionDependencies,
        {
            "timing": timing,
            "queue": queue,
            "runner": runner,
            "artifacts": artifacts,
            "tracking": tracking,
            "config": config,
            "admission": admission,
            "context": context,
        },
        execute_queue_entry_fn=execute_queue_entry_fn,
    )


def _worker_process_factory_callbacks() -> _worker_dependencies.WorkerProcessDependencyCallbacks:
    return _worker_dependencies.build_worker_process_dependency_callbacks(
        terminate_process=_terminate_process,
        wait_for_cancellable_process=_queue_execution.wait_for_cancellable_process,
        sleep=time.sleep,
        now_utc_iso=now_utc_iso,
        get_cancel_requested=get_cancel_requested,
        mark_completed=mark_completed,
        mark_cancelled=mark_cancelled,
        mark_failed=mark_failed,
        engine_runner_dependencies={
            "start_crest_job": start_crest_job,
            "finalize_crest_job": finalize_crest_job,
        },
    )


def _worker_execution_default_factories() -> dict[str, Callable[[], Any]]:
    return {
        **_worker_dependencies.build_worker_process_default_factories_from_callbacks(
            _worker_process_factory_callbacks(),
            config_factory=_default_config_dependencies,
            admission_factory=_default_admission_dependencies,
            runner_dependencies_type=WorkerRunnerDependencies,
            cancel_check_interval_seconds=CANCEL_CHECK_INTERVAL_SECONDS,
        ),
        "context": _default_context_dependencies,
        "artifacts": _default_artifact_dependencies,
        "tracking": _default_tracking_dependencies,
    }


_queue_entry_by_id = _worker_dependencies.build_queue_entry_lookup(
    list_queue_fn=lambda root: list_queue(root),
)


def _default_config_dependencies() -> WorkerConfigDependencies:
    return _worker_dependencies.build_worker_config_dependencies(
        load_config=load_config,
        queue_entry_by_id_fn=_queue_entry_by_id,
    )


def _default_admission_dependencies() -> WorkerAdmissionDependencies:
    return _worker_dependencies.build_worker_admission_dependencies(
        activate_reserved_slot=activate_reserved_slot,
        release_slot=release_slot,
    )


def _job_dir(entry: Any) -> Path:
    return _engine_execution.entry_metadata_resolved_path(entry, "job_dir")


def _selected_xyz(entry: Any) -> Path:
    return _engine_execution.entry_metadata_resolved_path(entry, "selected_input_xyz")


def _default_context_dependencies() -> WorkerContextDependencies:
    return WorkerContextDependencies(
        job_dir=_job_dir,
        selected_xyz=_selected_xyz,
        molecule_key=_molecule_key,
        mode=_mode,
        entry_resource_request=_queue_artifacts.entry_resource_request,
    )


def _default_artifact_dependencies() -> WorkerArtifactDependencies:
    return WorkerArtifactDependencies(
        write_running_state=_write_running_state,
        write_execution_artifacts=_write_execution_artifacts,
    )


def _default_tracking_dependencies() -> WorkerTrackingDependencies:
    return WorkerTrackingDependencies(
        upsert_job_record=upsert_job_record,
        notify_job_started=notify_job_started,
        notify_job_finished=notify_job_finished,
    )


def build_worker_execution_dependencies(
    *,
    config: WorkerConfigDependencies | None = None,
    admission: WorkerAdmissionDependencies | None = None,
    timing: WorkerTimingDependencies | None = None,
    queue: WorkerQueueDependencies | None = None,
    context: WorkerContextDependencies | None = None,
    runner: WorkerRunnerDependencies | None = None,
    artifacts: WorkerArtifactDependencies | None = None,
    tracking: WorkerTrackingDependencies | None = None,
    execute_queue_entry_fn: Callable[..., Any] | None = None,
) -> WorkerExecutionDependencies:
    return _worker_dependencies.build_worker_execution_dependency_container(
        build_worker_execution_dependencies_from_groups,
        {
            "config": config,
            "admission": admission,
            "timing": timing,
            "queue": queue,
            "context": context,
            "runner": runner,
            "artifacts": artifacts,
            "tracking": tracking,
        },
        _worker_execution_default_factories(),
        execute_queue_entry_fn=execute_queue_entry_fn,
    )


def default_worker_execution_dependencies() -> WorkerExecutionDependencies:
    return build_worker_execution_dependencies()


def publish_pending_cancel(entry: Any, *, config_path: str) -> None:
    cfg = load_config(config_path)
    dependencies = default_worker_execution_dependencies()
    context = _build_execution_context(
        cfg,
        entry,
        dependencies=dependencies,
        verify_execution_snapshot=False,
    )
    result = _engine_execution.build_terminal_result_from_context(
        _queue_artifacts.build_terminal_result,
        context,
        identity_fields=_engine_execution.object_attribute_fields(context, "mode"),
        status="cancelled",
        reason="cancel_requested",
        exit_code=1,
    )
    pending_state = _engine_execution.require_pending_cancel_state_for_entry(
        load_state(context.job_dir),
        entry=entry,
        engine="crest",
        job_dir=context.job_dir,
    )
    if not state_matches_job(
        pending_state,
        selected_input_xyz=context.selected_xyz,
        mode=context.mode,
        molecule_key=context.molecule_key,
    ):
        raise ValueError("Pending cancellation state does not match the queued CREST job")
    pending_status = pending_state.get("status")
    already_cancelled = (
        str(pending_status.get("state") or "").strip().lower() == "cancelled"
        if isinstance(pending_status, dict)
        else False
    )
    if not already_cancelled:
        dependencies.artifacts.write_execution_artifacts(entry, result)
    _terminal_sync_job_tracking(
        cfg,
        context,
        result,
        tracking_deps=dependencies.tracking,
    )


def _write_execution_artifacts(entry: Any, result: CrestRunResult) -> None:
    _terminal_write_execution_artifacts(
        entry,
        result,
        load_state_fn=load_state,
        state_matches_job_fn=state_matches_job,
        write_state_fn=write_state,
    )


def _write_running_state(cfg: Any, entry: Any) -> None:
    _terminal_write_running_state(
        cfg,
        entry,
        load_state_fn=load_state,
        state_matches_job_fn=state_matches_job,
        is_recovery_pending_fn=is_recovery_pending,
        write_state_fn=write_state,
        now_utc_iso_fn=_queue_artifacts.depsafe_now_utc_iso,
    )


def _terminate_process(proc: subprocess.Popen[str]) -> bool:
    return terminate_process_group(
        proc,
        killpg_fn=os.killpg,
        sigterm=signal.SIGTERM,
        sigkill=signal.SIGKILL,
    )


def _build_execution_context(
    cfg: Any,
    entry: Any,
    *,
    dependencies: WorkerExecutionDependencies,
    molecule_key_resolver: Callable[[Any, Path, Path], str] | None = None,
    verify_execution_snapshot: bool = True,
) -> ExecutionContext:
    context_deps = dependencies.context
    job_dir = _engine_execution.require_path_within_roots(
        context_deps.job_dir(entry),
        runtime_roots_for_cfg(cfg),
        label="Queue metadata 'job_dir'",
    )
    selected_xyz = _engine_execution.require_path_within_root(
        context_deps.selected_xyz(entry),
        job_dir,
        label="Queue metadata 'selected_input_xyz'",
    )
    resolve_molecule_key = molecule_key_resolver or context_deps.molecule_key
    resource_request = context_deps.entry_resource_request(cfg, entry)
    snapshot = _engine_execution.entry_metadata_dict(entry, "execution_snapshot")
    if verify_execution_snapshot and snapshot.get("version") != 1:
        raise ValueError("Queue metadata 'execution_snapshot' has an unsupported version")
    manifest_snapshot = snapshot.get("manifest")
    input_snapshots = snapshot.get("input_snapshots")
    if verify_execution_snapshot and (
        not isinstance(manifest_snapshot, dict) or not isinstance(input_snapshots, dict)
    ):
        raise ValueError("Queue metadata 'execution_snapshot' is incomplete")
    resolved_mode = context_deps.mode(entry)
    resolved_molecule_key = resolve_molecule_key(entry, selected_xyz, job_dir)
    if verify_execution_snapshot:
        assert isinstance(input_snapshots, dict)
        verified_inputs = verify_input_snapshots(job_dir, input_snapshots)
        if verified_inputs.get("selected") != selected_xyz:
            raise ValueError("Queued selected CREST input does not match its immutable snapshot")
        manifest_path = verified_inputs.get("manifest")
        if (
            not isinstance(manifest_snapshot, dict)
            or manifest_path is None
            or str(snapshot.get("manifest_path") or "") != str(manifest_path)
            or read_stable_regular_file(manifest_path, require_single_link=True)
            != json.dumps(
                manifest_snapshot,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ):
            raise ValueError("Queued CREST manifest payload does not match its immutable snapshot")
        if str(snapshot.get("selected_input_xyz") or "") != str(selected_xyz):
            raise ValueError("Queued CREST execution snapshot has a mismatched selected input")
        if str(snapshot.get("mode") or "") != resolved_mode:
            raise ValueError("Queued CREST execution snapshot has a mismatched mode")
        if str(snapshot.get("molecule_key") or "") != resolved_molecule_key:
            raise ValueError("Queued CREST execution snapshot has a mismatched molecule key")
        if snapshot.get("resource_request") != resource_request:
            raise ValueError("Queued CREST execution snapshot has a mismatched resource request")
    return ExecutionContext(
        entry=entry,
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        molecule_key=resolved_molecule_key,
        mode=resolved_mode,
        resource_request=resource_request,
        execution_snapshot=snapshot,
    )


def _mark_recovery_pending_context(cfg: Any, context: ExecutionContext, *, reason: str) -> None:
    identity_fields = _engine_execution.object_attribute_fields(
        context,
        "mode",
        "molecule_key",
    )
    _engine_execution.mark_recovery_pending_and_record(
        cfg,
        entry=context.entry,
        job_dir=context.job_dir,
        selected_input_xyz=context.selected_xyz,
        reason=reason,
        resource_request=context.resource_request,
        mark_recovery_pending_fn=mark_recovery_pending,
        upsert_job_record_fn=upsert_job_record,
        state_identity_fields=identity_fields,
        record_identity_fields=identity_fields,
    )


def _mark_recovery_pending_entry(cfg: Any, entry: Any, *, reason: str) -> None:
    context = _build_execution_context(
        cfg,
        entry,
        dependencies=default_worker_execution_dependencies(),
        verify_execution_snapshot=False,
    )
    _mark_recovery_pending_context(cfg, context, reason=reason)


def _mark_queue_terminal(
    queue_root: str | Path,
    context: ExecutionContext,
    result: CrestRunResult,
    *,
    dependencies: WorkerExecutionDependencies,
) -> None:
    _terminal_mark_queue_terminal(
        queue_root,
        context,
        result,
        queue_deps=dependencies.queue,
    )


def _raise_if_shutdown_requested(
    context: ExecutionContext,
    shutdown_requested: Callable[[], bool] | None,
) -> None:
    _engine_execution.raise_if_shutdown_callback_requested(
        context,
        shutdown_exception_type=WorkerShutdownRequested,
        shutdown_requested=shutdown_requested,
    )


def _mark_job_running(
    cfg: Any,
    context: ExecutionContext,
    *,
    dependencies: WorkerExecutionDependencies,
) -> None:
    _terminal_mark_job_running(
        cfg,
        context,
        artifact_deps=dependencies.artifacts,
        tracking_deps=dependencies.tracking,
    )


def _failed_result_from_exception(
    context: ExecutionContext,
    *,
    exc: Exception,
    failure_time: str,
) -> CrestRunResult:
    return _engine_execution.build_terminal_result_from_context(
        _queue_artifacts.build_terminal_result,
        context,
        identity_fields=_engine_execution.object_attribute_fields(context, "mode"),
        status="failed",
        reason=f"runner_error:{exc}",
        exit_code=1,
        now_utc_iso=failure_time,
    )


def _run_crest_job_for_entry(
    cfg: Any,
    context: ExecutionContext,
    *,
    queue_root: Path,
    dependencies: WorkerExecutionDependencies,
    shutdown_requested: Callable[[], bool] | None,
    prepare_running_job: Callable[[], None] | None,
    register_running_job: Callable[[Any | None], None] | None,
) -> CrestRunResult:
    queue_deps = dependencies.queue
    runner_deps = dependencies.runner

    def start_job() -> Any:
        launch_kwargs: dict[str, Any] = {}
        if prepare_running_job is not None:
            launch_kwargs["before_popen"] = prepare_running_job
        if register_running_job is not None:
            launch_kwargs["on_launch_aborted"] = lambda: register_running_job(None)
        return runner_deps.start_crest_job(
            cfg,
            job_dir=context.job_dir,
            selected_xyz=context.selected_xyz,
            execution_snapshot=context.execution_snapshot,
            **launch_kwargs,
        )

    return _engine_execution.run_engine_worker_process_job(
        context,
        options=_engine_execution.EngineWorkerOptions(
            should_cancel=_engine_execution.queue_cancel_callback(
                queue_deps,
                queue_root,
                context.entry,
            ),
            shutdown_requested=shutdown_requested,
            register_running_job=register_running_job,
        ),
        process_deps=runner_deps,
        shutdown_exception_type=WorkerShutdownRequested,
        start_job=start_job,
        finalize_job=runner_deps.finalize_crest_job,
        build_failure_result=lambda exc: _failed_result_from_exception(
            context,
            exc=exc,
            failure_time=dependencies.timing.now_utc_iso(),
        ),
    )


def _finalize_processed_entry(
    cfg: Any,
    context: ExecutionContext,
    result: CrestRunResult,
    *,
    queue_root: Path,
    dependencies: WorkerExecutionDependencies,
) -> Path | None:
    output_identity_matches = True
    output_identities: dict[str, dict[str, Any]] = {}
    expected_output_identities = dict(result.output_identities)
    try:
        evidence_paths = (
            {
                *result.retained_conformer_paths,
                result.stdout_log,
                result.stderr_log,
            }
            if result.status == "completed" or expected_output_identities
            else set()
        )
        for path in sorted(path for path in evidence_paths if path):
            identity = _engine_runner.confined_output_identity(context.job_dir, path)
            output_identities[str(identity["path"])] = identity
    except (OSError, RuntimeError, TypeError, ValueError):
        output_identity_matches = False
    if expected_output_identities and output_identities != expected_output_identities:
        output_identity_matches = False
    result = dataclasses.replace(result, output_identities=output_identities)
    snapshot_inputs = context.execution_snapshot.get("input_snapshots")
    identity_matches = isinstance(snapshot_inputs, dict)
    if isinstance(snapshot_inputs, dict):
        try:
            verify_input_snapshots(context.job_dir, snapshot_inputs)
        except (OSError, RuntimeError, TypeError, ValueError):
            identity_matches = False
    executable_identities = context.execution_snapshot.get("executable_identities")
    if isinstance(executable_identities, dict):
        try:
            _engine_runner.verify_executable_identity(executable_identities.get("crest"))
            _engine_runner.verify_executable_identity(executable_identities.get("xtb"))
        except (OSError, RuntimeError, TypeError, ValueError):
            identity_matches = False
    else:
        identity_matches = False
    identity_matches = (
        identity_matches
        and output_identity_matches
        and all(
            (
                str(getattr(result, "selected_input_xyz", "")) == str(context.selected_xyz),
                str(getattr(result, "mode", "")) == context.mode,
                getattr(result, "resource_request", None) == context.resource_request,
            )
        )
    )
    if not identity_matches and dataclasses.is_dataclass(result):
        result = dataclasses.replace(
            result,
            status="failed",
            reason="terminal_identity_mismatch",
            exit_code=1,
            selected_input_xyz=str(context.selected_xyz),
            mode=context.mode,
            resource_request=dict(context.resource_request),
        )
    return _terminal_finalize_processed_entry(
        cfg,
        context,
        result,
        queue_root=queue_root,
        dependencies=dependencies,
    )


def _worker_execution_spec(
    *,
    molecule_key_resolver: Callable[[Any, Path, Path], str],
    dependencies: WorkerExecutionDependencies,
) -> _engine_execution.EngineWorkerExecutionSpec:
    return _engine_execution.build_engine_worker_execution_spec(
        build_context=lambda cfg_obj, entry_obj: _build_execution_context(
            cfg_obj,
            entry_obj,
            dependencies=dependencies,
            molecule_key_resolver=molecule_key_resolver,
        ),
        shutdown_exception_type=WorkerShutdownRequested,
        mark_running=lambda cfg_obj, context, _options: _mark_job_running(
            cfg_obj,
            context,
            dependencies=dependencies,
        ),
        run_job=lambda cfg_obj, context, active_queue_root, options: _run_crest_job_for_entry(
            cfg_obj,
            context,
            queue_root=active_queue_root,
            dependencies=dependencies,
            shutdown_requested=options.shutdown_requested,
            prepare_running_job=options.prepare_running_job,
            register_running_job=options.register_running_job,
        ),
        finalize_entry=lambda cfg_obj, context, result, active_queue_root, _options: (
            _finalize_processed_entry(
                cfg_obj,
                context,
                result,
                queue_root=active_queue_root,
                dependencies=dependencies,
            )
        ),
        build_outcome=lambda context, result, sync_result: WorkerExecutionOutcome(
            result=result,
            job_dir=context.job_dir,
            selected_xyz=context.selected_xyz,
            molecule_key=context.molecule_key,
        ),
    )


def _run_worker_entry_lifecycle(
    cfg: Any,
    entry: Any,
    *,
    queue_root: Path | None,
    molecule_key_resolver: Callable[[Any, Path, Path], str],
    dependencies: WorkerExecutionDependencies,
    shutdown_requested: Callable[[], bool] | None = None,
    prepare_running_job: Callable[[], None] | None = None,
    register_running_job: Callable[[Any | None], None] | None = None,
    worker_job_pid: int | None = None,
    emit_output: bool = False,
) -> WorkerExecutionOutcome:
    return _engine_execution.run_engine_worker_entry_with_spec_factory_options(
        cfg,
        entry,
        queue_root=queue_root,
        spec_factory=lambda: _worker_execution_spec(
            molecule_key_resolver=molecule_key_resolver,
            dependencies=dependencies,
        ),
        shutdown_requested=shutdown_requested,
        prepare_running_job=prepare_running_job,
        register_running_job=register_running_job,
        worker_job_pid=worker_job_pid,
        emit_output=emit_output,
    )


def execute_queue_entry(
    cfg: Any,
    *,
    queue_root: Path,
    entry: Any,
    molecule_key_resolver: Callable[[Any, Path, Path], str] = _molecule_key,
    dependencies: WorkerExecutionDependencies | None = None,
    shutdown_requested: Callable[[], bool] | None = None,
    prepare_running_job: Callable[[], None] | None = None,
    register_running_job: Callable[[Any | None], None] | None = None,
    worker_job_pid: int | None = None,
    emit_output: bool = False,
) -> WorkerExecutionOutcome:
    return _run_worker_entry_lifecycle(
        cfg,
        entry,
        queue_root=queue_root,
        molecule_key_resolver=molecule_key_resolver,
        dependencies=dependencies or default_worker_execution_dependencies(),
        shutdown_requested=shutdown_requested,
        prepare_running_job=prepare_running_job,
        register_running_job=register_running_job,
        worker_job_pid=worker_job_pid,
        emit_output=emit_output,
    )


def process_dequeued_entry(
    cfg: Any,
    entry: Any,
    *,
    queue_root: Path | None = None,
    molecule_key_resolver: Callable[[Any, Path, Path], str] | None = None,
    dependencies: WorkerExecutionDependencies | None = None,
    shutdown_requested: Callable[[], bool] | None = None,
    prepare_running_job: Callable[[], None] | None = None,
    register_running_job: Callable[[Any | None], None] | None = None,
) -> WorkerExecutionOutcome:
    deps = dependencies or default_worker_execution_dependencies()
    return _run_worker_entry_lifecycle(
        cfg,
        entry,
        queue_root=queue_root,
        molecule_key_resolver=molecule_key_resolver or deps.context.molecule_key,
        dependencies=deps,
        shutdown_requested=shutdown_requested,
        prepare_running_job=prepare_running_job,
        register_running_job=register_running_job,
    )


def _admission_root_for_cfg(cfg: Any) -> str:
    return resolve_admission_root(cfg)


def _find_queue_entry(queue_root: Path, queue_id: str) -> Any | None:
    return _queue_entry_by_id(queue_root, queue_id)


def run_worker_child_job(
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    admission_token: str | None = None,
    dependencies: WorkerExecutionDependencies | None = None,
) -> int:
    deps = dependencies or default_worker_execution_dependencies()
    return run_engine_worker_child_job(
        spec=_WORKER_CHILD_RUN_SPEC,
        config_path=config_path,
        queue_root=queue_root,
        queue_id=queue_id,
        admission_token=admission_token,
        load_config_fn=deps.config.load_config,
        find_queue_entry_fn=deps.config.queue_entry_by_id,
        admission_root_fn=_admission_root_for_cfg,
        install_signal_handlers_fn=lambda controller: install_shutdown_request_handlers(
            controller,
            install_signal_handlers_fn=install_shutdown_signal_handlers,
        ),
        process_dequeued_entry_fn=process_dequeued_entry,
        dependencies_fn=lambda: deps,
        requeue_running_entry_fn=requeue_running_entry,
        mark_recovery_pending_context_fn=_mark_recovery_pending_context,
        process_dequeued_entry_kwargs={"molecule_key_resolver": _molecule_key},
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orca_auto.flow.engines.crest.execution")
    parser.add_argument("--config", required=True)
    parser.add_argument("--queue-root", required=True)
    parser.add_argument("--queue-id", required=True)
    parser.add_argument("--admission-token", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_worker_child_job(
        config_path=args.config,
        queue_root=args.queue_root,
        queue_id=args.queue_id,
        admission_token=str(args.admission_token).strip() or None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
