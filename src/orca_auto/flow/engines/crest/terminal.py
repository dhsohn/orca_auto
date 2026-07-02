from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.queue import engine_execution as _engine_execution
from orca_auto.core.queue import execution as _queue_execution
from orca_auto.flow.engines.crest import artifacts as _queue_artifacts
from orca_auto.flow.engines.crest.runner import CrestRunResult
from orca_auto.flow.engines.crest.worker_context import ExecutionContext


@dataclass(frozen=True)
class WorkerExecutionOutcome:
    result: CrestRunResult
    job_dir: Path
    selected_xyz: Path
    molecule_key: str
    organized_output_dir: Path | None


def write_execution_artifacts(
    entry: Any,
    result: CrestRunResult,
    *,
    load_state_fn: Callable[..., Any],
    state_matches_job_fn: Callable[..., Any],
    write_state_fn: Callable[..., Any],
    write_report_json_fn: Callable[..., Any],
    write_report_md_lines_fn: Callable[..., Any],
) -> None:
    _queue_artifacts.write_execution_artifacts(
        entry,
        result,
        load_state_fn=load_state_fn,
        state_matches_job_fn=state_matches_job_fn,
        write_state_fn=write_state_fn,
        write_report_json_fn=write_report_json_fn,
        write_report_md_lines_fn=write_report_md_lines_fn,
    )


def write_running_state(
    cfg: Any,
    entry: Any,
    *,
    load_state_fn: Callable[..., Any],
    state_matches_job_fn: Callable[..., Any],
    is_recovery_pending_fn: Callable[..., Any],
    write_state_fn: Callable[..., Any],
    now_utc_iso_fn: Callable[[], str],
) -> None:
    _queue_artifacts.write_running_state(
        cfg,
        entry,
        load_state_fn=load_state_fn,
        state_matches_job_fn=state_matches_job_fn,
        is_recovery_pending_fn=is_recovery_pending_fn,
        write_state_fn=write_state_fn,
        now_utc_iso_fn=now_utc_iso_fn,
    )


def mark_queue_terminal(
    queue_root: str | Path,
    context: ExecutionContext,
    result: CrestRunResult,
    *,
    queue_deps: Any,
) -> None:
    _engine_execution.mark_result_terminal_status(
        queue_root,
        context.entry.queue_id,
        result,
        metadata_update={
            "retained_conformer_count": result.retained_conformer_count,
            "mode": result.mode,
        },
        mark_terminal_status_fn=_queue_execution.mark_terminal_status,
        mark_completed_fn=queue_deps.mark_completed,
        mark_cancelled_fn=queue_deps.mark_cancelled,
        mark_failed_fn=queue_deps.mark_failed,
    )


def sync_job_tracking(
    cfg: Any,
    context: ExecutionContext,
    result: CrestRunResult,
    *,
    tracking_deps: Any,
) -> Path | None:
    tracking_deps.upsert_job_record(
        cfg,
        job_id=context.entry.task_id,
        status=result.status,
        job_dir=context.job_dir,
        mode=result.mode,
        selected_input_xyz=str(context.selected_xyz),
        molecule_key=context.molecule_key,
        resource_request=result.resource_request,
        resource_actual=result.resource_actual,
    )
    return None


def mark_job_running(
    cfg: Any,
    context: ExecutionContext,
    *,
    artifact_deps: Any,
    tracking_deps: Any,
) -> None:
    _engine_execution.mark_engine_job_running(
        cfg,
        entry=context.entry,
        job_dir=context.job_dir,
        selected_xyz=context.selected_xyz,
        resource_request=context.resource_request,
        write_running_state_fn=artifact_deps.write_running_state,
        upsert_job_record_fn=tracking_deps.upsert_job_record,
        notify_job_started_fn=tracking_deps.notify_job_started,
        record_fields={
            "mode": context.mode,
            "molecule_key": context.molecule_key,
        },
        notify_fields={
            "mode": context.mode,
        },
    )


def finalize_processed_entry(
    cfg: Any,
    context: ExecutionContext,
    result: CrestRunResult,
    *,
    queue_root: Path,
    dependencies: Any,
) -> Path | None:
    artifact_deps = dependencies.artifacts
    tracking_deps = dependencies.tracking

    def notify_finished(organized_output_dir: Path | None) -> None:
        tracking_deps.notify_job_finished(
            cfg,
            job_id=context.entry.task_id,
            queue_id=context.entry.queue_id,
            status=result.status,
            reason=result.reason,
            mode=result.mode,
            job_dir=context.job_dir,
            selected_xyz=context.selected_xyz,
            retained_conformer_count=result.retained_conformer_count,
            organized_output_dir=organized_output_dir,
            resource_request=context.resource_request,
            resource_actual=result.resource_actual,
        )

    return _engine_execution.sync_terminal_result(
        _engine_execution.TerminalSyncActions(
            write_artifacts=lambda: artifact_deps.write_execution_artifacts(
                context.entry,
                result,
            ),
            mark_queue_terminal=lambda: mark_queue_terminal(
                queue_root,
                context,
                result,
                queue_deps=dependencies.queue,
            ),
            sync_job_record=lambda: sync_job_tracking(
                cfg,
                context,
                result,
                tracking_deps=tracking_deps,
            ),
            notify_finished=notify_finished,
            build_outcome=lambda organized_output_dir: organized_output_dir,
        ),
    )


__all__ = [
    "WorkerExecutionOutcome",
    "finalize_processed_entry",
    "mark_job_running",
    "mark_queue_terminal",
    "sync_job_tracking",
    "write_execution_artifacts",
    "write_running_state",
]
