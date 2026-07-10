from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.admission import activate_reserved_slot, release_slot
from orca_auto.core.config.engines import load_xtb_config as load_config
from orca_auto.core.engines.worker_child import (
    WORKER_CHILD_MODULE,
    build_worker_child_command_for_engine,
)
from orca_auto.core.notifications.engines import (
    notify_xtb_job_started as notify_job_started,
)
from orca_auto.core.queue import execution as _queue_execution
from orca_auto.core.queue import (
    get_cancel_requested,
    list_queue,
    mark_cancelled,
    mark_completed,
    mark_failed,
    requeue_running_entry,
)
from orca_auto.core.queue.engine import execution as _engine_execution
from orca_auto.core.queue.internal_engine import (
    InternalEngineSpec,
    create_worker_shutdown_exception_type,
)
from orca_auto.core.queue.worker import execution_dependencies as _worker_dependencies
from orca_auto.core.queue.worker import (
    install_shutdown_signal_handlers,
    resolve_admission_root,
    terminate_process_group,
)
from orca_auto.core.utils import now_utc_iso
from orca_auto.flow.engines.xtb import artifacts as _queue_artifacts
from orca_auto.flow.engines.xtb.job_locations import upsert_job_record
from orca_auto.flow.engines.xtb.runner import (
    finalize_xtb_job,
    run_path_search_ts_hessian_followup,
    run_xtb_ranking_job,
    start_xtb_job,
)
from orca_auto.flow.engines.xtb.state import (
    is_recovery_pending,
    mark_recovery_pending,
)
from orca_auto.flow.engines.xtb.worker_context import (
    WorkerExecutionHooks,
    default_worker_execution_hooks,
)
from orca_auto.flow.engines.xtb.worker_context import (
    XtbExecutionContext as _XtbExecutionContext,
)
from orca_auto.flow.engines.xtb.worker_context import (
    build_execution_context as _build_worker_execution_context,
)
from orca_auto.flow.engines.xtb.worker_context import (
    input_summary as _input_summary,
)
from orca_auto.flow.engines.xtb.worker_context import (
    job_dir as _job_dir,
)
from orca_auto.flow.engines.xtb.worker_context import (
    job_type as _job_type,
)
from orca_auto.flow.engines.xtb.worker_context import (
    matching_state as _matching_state,
)
from orca_auto.flow.engines.xtb.worker_context import (
    reaction_key as _reaction_key,
)
from orca_auto.flow.engines.xtb.worker_context import (
    selected_xyz as _selected_xyz,
)
from orca_auto.flow.engines.xtb.worker_terminal import (
    WorkerExecutionOutcome,
)
from orca_auto.flow.engines.xtb.worker_terminal import (
    build_terminal_result as _build_terminal_result,
)
from orca_auto.flow.engines.xtb.worker_terminal import (
    finalize_execution_result as _finalize_execution_result,
)
from orca_auto.flow.engines.xtb.worker_terminal import (
    write_running_state as _write_running_state,
)

WORKER_JOB_MODULE = WORKER_CHILD_MODULE
CANCEL_CHECK_INTERVAL_SECONDS = 1
WorkerShutdownRequested = create_worker_shutdown_exception_type(__name__)
_ENGINE_SPEC = InternalEngineSpec(
    engine="xtb",
    worker_job_module="orca_auto.flow.engines.xtb.execution",
    include_admission_root=False,
)
build_worker_child_command = build_worker_child_command_for_engine("xtb")


_worker_child = _ENGINE_SPEC.worker_child_module_facade(
    WorkerShutdownRequested,
    build_worker_child_command=build_worker_child_command,
)
_WORKER_CHILD = _worker_child.worker_child


WorkerConfigDependencies = _worker_dependencies.WorkerConfigDependencies
WorkerAdmissionDependencies = _worker_dependencies.WorkerAdmissionDependencies
WorkerTimingDependencies = _engine_execution.InternalWorkerTimingDependencies
WorkerQueueDependencies = _engine_execution.InternalWorkerQueueDependencies


@dataclass(frozen=True)
class WorkerContextDependencies:
    job_dir: Callable[[Any], Path]
    selected_xyz: Callable[[Any], Path]
    job_type: Callable[[Any], str]
    reaction_key: Callable[[Any, Path], str]
    input_summary: Callable[[Any], dict[str, Any]]
    entry_resource_request: Callable[[Any, Any], dict[str, int]]
    matching_state: Callable[..., dict[str, Any]]
    is_recovery_pending: Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class WorkerArtifactDependencies:
    write_running_state: Callable[..., Any]
    build_terminal_result: Callable[..., Any]
    finalize_execution_result: Callable[..., Any]


@dataclass(frozen=True)
class WorkerTrackingDependencies:
    upsert_job_record: Callable[..., Any]
    notify_job_started: Callable[..., Any]


@dataclass(frozen=True)
class WorkerRunnerDependencies(_engine_execution.InternalWorkerProcessDependencies):
    run_xtb_ranking_job: Callable[..., Any]
    start_xtb_job: Callable[..., Any]
    finalize_xtb_job: Callable[..., Any]
    run_path_search_ts_hessian_followup: Callable[..., Any]


@dataclass(frozen=True)
class WorkerExecutionDependencies:
    config: WorkerConfigDependencies
    admission: WorkerAdmissionDependencies
    timing: WorkerTimingDependencies
    queue: WorkerQueueDependencies
    context: WorkerContextDependencies
    artifacts: WorkerArtifactDependencies
    tracking: WorkerTrackingDependencies
    runner: WorkerRunnerDependencies
    execute_queue_entry: Callable[..., Any] | None = None


def build_worker_execution_dependencies_from_groups(
    *,
    config: WorkerConfigDependencies,
    admission: WorkerAdmissionDependencies,
    timing: WorkerTimingDependencies,
    queue: WorkerQueueDependencies,
    context: WorkerContextDependencies,
    artifacts: WorkerArtifactDependencies,
    tracking: WorkerTrackingDependencies,
    runner: WorkerRunnerDependencies,
    execute_queue_entry_fn: Callable[..., Any] | None = None,
) -> WorkerExecutionDependencies:
    return _worker_dependencies.build_worker_execution_dependencies_from_groups(
        WorkerExecutionDependencies,
        {
            "config": config,
            "admission": admission,
            "timing": timing,
            "queue": queue,
            "context": context,
            "artifacts": artifacts,
            "tracking": tracking,
            "runner": runner,
        },
        execute_queue_entry_fn=execute_queue_entry_fn,
    )


def _worker_process_factory_callbacks() -> _worker_dependencies.WorkerProcessDependencyCallbacks:
    return _worker_dependencies.build_worker_process_dependency_callbacks(
        terminate_process=terminate_process_group,
        wait_for_cancellable_process=_queue_execution.wait_for_cancellable_process,
        sleep=time.sleep,
        now_utc_iso=now_utc_iso,
        get_cancel_requested=get_cancel_requested,
        mark_completed=mark_completed,
        mark_cancelled=mark_cancelled,
        mark_failed=mark_failed,
        engine_runner_dependencies={
            "run_xtb_ranking_job": run_xtb_ranking_job,
            "start_xtb_job": start_xtb_job,
            "finalize_xtb_job": finalize_xtb_job,
            "run_path_search_ts_hessian_followup": run_path_search_ts_hessian_followup,
        },
    )


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


def _default_context_dependencies() -> WorkerContextDependencies:
    return WorkerContextDependencies(
        job_dir=_job_dir,
        selected_xyz=_selected_xyz,
        job_type=_job_type,
        reaction_key=_reaction_key,
        input_summary=_input_summary,
        entry_resource_request=_queue_artifacts.entry_resource_request,
        matching_state=_matching_state,
        is_recovery_pending=is_recovery_pending,
    )


def _default_artifact_dependencies() -> WorkerArtifactDependencies:
    return WorkerArtifactDependencies(
        write_running_state=_write_running_state,
        build_terminal_result=_build_terminal_result,
        finalize_execution_result=_finalize_execution_result,
    )


def _default_tracking_dependencies() -> WorkerTrackingDependencies:
    return WorkerTrackingDependencies(
        upsert_job_record=upsert_job_record,
        notify_job_started=notify_job_started,
    )


def _worker_execution_default_factories() -> dict[str, Callable[[], Any]]:
    return {
        **_worker_dependencies.build_worker_process_default_factories_from_callbacks(
            _worker_process_factory_callbacks(),
            config_factory=_default_config_dependencies,
            admission_factory=_default_admission_dependencies,
            timing_dependencies_type=WorkerTimingDependencies,
            queue_dependencies_type=WorkerQueueDependencies,
            runner_dependencies_type=WorkerRunnerDependencies,
            cancel_check_interval_seconds=CANCEL_CHECK_INTERVAL_SECONDS,
        ),
        "context": _default_context_dependencies,
        "artifacts": _default_artifact_dependencies,
        "tracking": _default_tracking_dependencies,
    }


def build_worker_execution_dependencies(
    *,
    config: WorkerConfigDependencies | None = None,
    admission: WorkerAdmissionDependencies | None = None,
    timing: WorkerTimingDependencies | None = None,
    queue: WorkerQueueDependencies | None = None,
    context: WorkerContextDependencies | None = None,
    artifacts: WorkerArtifactDependencies | None = None,
    tracking: WorkerTrackingDependencies | None = None,
    runner: WorkerRunnerDependencies | None = None,
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
            "artifacts": artifacts,
            "tracking": tracking,
            "runner": runner,
        },
        _worker_execution_default_factories(),
        execute_queue_entry_fn=execute_queue_entry_fn,
    )


def default_worker_execution_dependencies() -> WorkerExecutionDependencies:
    return build_worker_execution_dependencies()


def _build_execution_context(
    cfg: Any,
    entry: Any,
    *,
    dependencies: WorkerExecutionDependencies,
) -> _XtbExecutionContext:
    return _build_worker_execution_context(
        cfg,
        entry,
        context_deps=dependencies.context,
    )


def _mark_job_running(
    cfg: Any,
    context: _XtbExecutionContext,
    *,
    worker_job_pid: int | None = None,
    dependencies: WorkerExecutionDependencies,
) -> None:
    artifact_deps = dependencies.artifacts
    tracking_deps = dependencies.tracking
    tracking_fields = _engine_execution.object_attribute_fields(
        context,
        "job_type",
        "reaction_key",
    )
    _engine_execution.mark_engine_job_running(
        cfg,
        entry=context.entry,
        job_dir=context.job_dir,
        selected_xyz=context.selected_xyz,
        resource_request=context.resource_request,
        write_running_state_fn=artifact_deps.write_running_state,
        upsert_job_record_fn=tracking_deps.upsert_job_record,
        notify_job_started_fn=tracking_deps.notify_job_started,
        record_fields=tracking_fields,
        notify_fields=tracking_fields,
        write_running_state_kwargs={
            "worker_job_pid": worker_job_pid,
            "previous_state": context.previous_state,
            "resumed": context.resumed,
        },
    )


def _raise_if_shutdown_requested(
    context: _XtbExecutionContext,
    shutdown_requested: Callable[[], bool] | None,
) -> None:
    _engine_execution.raise_if_shutdown_callback_requested(
        context,
        shutdown_exception_type=WorkerShutdownRequested,
        shutdown_requested=shutdown_requested,
    )


def _mark_recovery_pending_context(
    cfg: Any,
    context: _XtbExecutionContext,
    *,
    reason: str,
) -> None:
    tracking_fields = _engine_execution.object_attribute_fields(
        context,
        "job_type",
        "reaction_key",
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
        state_identity_fields={
            **tracking_fields,
            "input_summary": context.input_summary,
        },
        record_identity_fields=tracking_fields,
    )


def _mark_recovery_pending_entry(cfg: Any, entry: Any, *, reason: str) -> None:
    context = _build_execution_context(
        cfg,
        entry,
        dependencies=default_worker_execution_dependencies(),
    )
    _mark_recovery_pending_context(cfg, context, reason=reason)


def _cancelled_before_start_result(
    context: _XtbExecutionContext,
    *,
    dependencies: WorkerExecutionDependencies,
) -> Any:
    return _engine_execution.build_terminal_result_from_context(
        dependencies.artifacts.build_terminal_result,
        context,
        identity_fields=_engine_execution.object_attribute_fields(
            context,
            "job_type",
            "reaction_key",
            "input_summary",
        ),
        status="cancelled",
        reason="cancel_requested",
        exit_code=1,
    )


def _failed_result_from_exception(
    context: _XtbExecutionContext,
    exc: Exception,
    *,
    dependencies: WorkerExecutionDependencies,
) -> Any:
    return _engine_execution.build_terminal_result_from_context(
        dependencies.artifacts.build_terminal_result,
        context,
        identity_fields=_engine_execution.object_attribute_fields(
            context,
            "job_type",
            "reaction_key",
            "input_summary",
        ),
        status="failed",
        reason=f"runner_error:{exc}",
        exit_code=1,
    )


def _run_xtb_job_for_entry(
    cfg: Any,
    context: _XtbExecutionContext,
    _queue_root: Path,
    *,
    dependencies: WorkerExecutionDependencies,
    should_cancel: Callable[[], bool] | None,
    shutdown_requested: Callable[[], bool] | None = None,
    prepare_running_job: Callable[[], None] | None,
    register_running_job: Callable[[Any | None], None] | None,
) -> Any:
    runner_deps = dependencies.runner
    options = _engine_execution.InternalWorkerOptions(
        should_cancel=should_cancel,
        shutdown_requested=shutdown_requested,
        register_running_job=register_running_job,
    )

    def should_stop_ranking() -> bool:
        return (should_cancel is not None and should_cancel()) or (
            shutdown_requested is not None and shutdown_requested()
        )

    try:
        if should_cancel is not None and should_cancel():
            return _cancelled_before_start_result(context, dependencies=dependencies)
        _raise_if_shutdown_requested(context, shutdown_requested)
        if context.job_type == "ranking":
            result = runner_deps.run_xtb_ranking_job(
                cfg,
                job_dir=context.job_dir,
                should_cancel=should_stop_ranking,
                prepare_running_job=prepare_running_job,
                on_running_job=register_running_job,
                terminate_process=runner_deps.terminate_process,
            )
            _raise_if_shutdown_requested(context, shutdown_requested)
            return result

        def start_job() -> Any:
            launch_kwargs: dict[str, Any] = {}
            if prepare_running_job is not None:
                launch_kwargs["before_popen"] = prepare_running_job
            if register_running_job is not None:
                launch_kwargs["on_launch_aborted"] = lambda: register_running_job(None)
            return runner_deps.start_xtb_job(
                cfg,
                job_dir=context.job_dir,
                selected_input_xyz=context.selected_xyz,
                **launch_kwargs,
            )

        result = _engine_execution.run_internal_worker_process_job(
            context,
            options=options,
            process_deps=runner_deps,
            shutdown_exception_type=WorkerShutdownRequested,
            start_job=start_job,
            finalize_job=runner_deps.finalize_xtb_job,
            build_failure_result=lambda exc: _failed_result_from_exception(
                context,
                exc,
                dependencies=dependencies,
            ),
            check_cancel_before_poll=True,
        )
        if context.job_type == "path_search":
            result = runner_deps.run_path_search_ts_hessian_followup(
                cfg,
                result,
                job_dir=context.job_dir,
                should_cancel=should_stop_ranking,
                prepare_running_job=prepare_running_job,
                on_running_job=register_running_job,
                terminate_process=runner_deps.terminate_process,
            )
        return result
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, (WorkerShutdownRequested, _engine_execution.ProcessCleanupError)):
            raise
        return _failed_result_from_exception(context, exc, dependencies=dependencies)


def _finalize_processed_entry(
    cfg: Any,
    context: _XtbExecutionContext,
    result: Any,
    queue_root: Path,
    *,
    emit_output: bool,
    dependencies: WorkerExecutionDependencies,
) -> Any:
    return dependencies.artifacts.finalize_execution_result(
        cfg,
        queue_root=queue_root,
        entry=context.entry,
        result=result,
        emit_output=emit_output,
        previous_state=context.previous_state,
        resumed=context.resumed,
    )


def _worker_execution_spec(
    *,
    dependencies: WorkerExecutionDependencies,
    should_cancel_factory: Callable[
        [Path, _XtbExecutionContext],
        Callable[[], bool] | None,
    ],
) -> _engine_execution.InternalEngineWorkerExecutionSpec:
    return _engine_execution.build_internal_engine_worker_execution_spec(
        build_context=lambda cfg_obj, entry_obj: _build_execution_context(
            cfg_obj,
            entry_obj,
            dependencies=dependencies,
        ),
        mark_running=lambda cfg_obj, context, options: _mark_job_running(
            cfg_obj,
            context,
            worker_job_pid=options.worker_job_pid,
            dependencies=dependencies,
        ),
        shutdown_exception_type=WorkerShutdownRequested,
        run_job=lambda cfg_obj, context, active_queue_root, options: _run_xtb_job_for_entry(
            cfg_obj,
            context,
            active_queue_root,
            dependencies=dependencies,
            should_cancel=should_cancel_factory(active_queue_root, context),
            shutdown_requested=options.shutdown_requested,
            prepare_running_job=options.prepare_running_job,
            register_running_job=options.register_running_job,
        ),
        finalize_entry=lambda cfg_obj, context, result, active_queue_root, options: (
            _finalize_processed_entry(
                cfg_obj,
                context,
                result,
                active_queue_root,
                emit_output=options.emit_output,
                dependencies=dependencies,
            )
        ),
    )


def build_worker_adapter(
    *,
    dependencies: WorkerExecutionDependencies,
    should_cancel_factory: Callable[
        [Path, _XtbExecutionContext],
        Callable[[], bool] | None,
    ],
) -> _engine_execution.InternalEngineWorkerAdapter:
    return _engine_execution.build_internal_engine_worker_adapter_from_spec(
        _worker_execution_spec(
            dependencies=dependencies,
            should_cancel_factory=should_cancel_factory,
        )
    )


def _run_worker_entry_lifecycle(
    cfg: Any,
    entry: Any,
    *,
    queue_root: Path | None,
    dependencies: WorkerExecutionDependencies,
    should_cancel_factory: Callable[
        [Path, _XtbExecutionContext],
        Callable[[], bool] | None,
    ],
    shutdown_requested: Callable[[], bool] | None = None,
    prepare_running_job: Callable[[], None] | None = None,
    register_running_job: Callable[[Any | None], None] | None = None,
    worker_job_pid: int | None = None,
    emit_output: bool = False,
) -> Any:
    return _engine_execution.run_internal_engine_worker_entry_with_spec_factory_options(
        cfg,
        entry,
        queue_root=queue_root,
        spec_factory=lambda: _worker_execution_spec(
            dependencies=dependencies,
            should_cancel_factory=should_cancel_factory,
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
    should_cancel: Callable[[], bool] | None = None,
    shutdown_requested: Callable[[], bool] | None = None,
    prepare_running_job: Callable[[], None] | None = None,
    register_running_job: Callable[[Any | None], None] | None = None,
    worker_job_pid: int | None = None,
    emit_output: bool = False,
    dependencies: WorkerExecutionDependencies | None = None,
) -> Any:
    deps = dependencies or default_worker_execution_dependencies()
    return _run_worker_entry_lifecycle(
        cfg,
        entry,
        queue_root=queue_root,
        dependencies=deps,
        should_cancel_factory=lambda _active_queue_root, _context: should_cancel,
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
        dependencies=deps,
        should_cancel_factory=lambda active_queue_root, context: (
            _engine_execution.queue_cancel_callback(
                deps.queue,
                active_queue_root,
                context.entry,
            )
        ),
        shutdown_requested=shutdown_requested,
        prepare_running_job=prepare_running_job,
        register_running_job=register_running_job,
    )


def run_worker_job(
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    admission_token: str | None = None,
    dependencies: WorkerExecutionDependencies | None = None,
    await_parent_admission_handoff_fn: Callable[[Any, str], bool] | None = None,
) -> int:
    deps = dependencies or default_worker_execution_dependencies()
    return _worker_dependencies.run_worker_child_entrypoint_with_dependencies(
        _worker_child,
        config_path=config_path,
        queue_root=queue_root,
        queue_id=queue_id,
        admission_token=admission_token,
        admission_root_fn=resolve_admission_root,
        install_shutdown_signal_handlers_fn=install_shutdown_signal_handlers,
        process_dequeued_entry_fn=process_dequeued_entry,
        dependencies=deps,
        requeue_running_entry_fn=requeue_running_entry,
        mark_recovery_pending_context_fn=_mark_recovery_pending_context,
        await_parent_admission_handoff_fn=await_parent_admission_handoff_fn,
    )


def build_worker_job_parser() -> argparse.ArgumentParser:
    return _worker_child.build_parser()


run_worker_child_job = run_worker_job
build_parser = build_worker_job_parser
shutdown_signal_handler_installer = _WORKER_CHILD.shutdown_signal_handler_installer


def main(argv: list[str] | None = None) -> int:
    args = build_worker_job_parser().parse_args(argv)
    return run_worker_job(
        config_path=args.config,
        queue_root=args.queue_root,
        queue_id=args.queue_id,
        admission_token=str(args.admission_token).strip() or None,
    )


__all__ = [
    "build_worker_child_command",
    "build_worker_adapter",
    "build_worker_execution_dependencies",
    "build_worker_execution_dependencies_from_groups",
    "build_parser",
    "build_worker_job_parser",
    "WorkerAdmissionDependencies",
    "WorkerArtifactDependencies",
    "WorkerConfigDependencies",
    "WorkerContextDependencies",
    "WorkerExecutionDependencies",
    "WorkerExecutionHooks",
    "WorkerExecutionOutcome",
    "WorkerQueueDependencies",
    "WorkerRunnerDependencies",
    "WorkerTimingDependencies",
    "WorkerTrackingDependencies",
    "default_worker_execution_hooks",
    "default_worker_execution_dependencies",
    "execute_queue_entry",
    "main",
    "process_dequeued_entry",
    "run_worker_child_job",
    "run_worker_job",
    "shutdown_signal_handler_installer",
    "WorkerShutdownRequested",
    "WORKER_JOB_MODULE",
]


if __name__ == "__main__":
    raise SystemExit(main())
