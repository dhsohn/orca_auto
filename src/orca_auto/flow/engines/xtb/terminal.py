from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.core.queue import execution as _queue_execution
from orca_auto.core.queue.engine import execution as _engine_execution
from orca_auto.flow.engines.xtb.runner import XtbRunResult


@dataclass(frozen=True)
class TerminalSummary:
    queue_id: str
    job_id: str
    status: str
    reason: str
    organized_output_dir: str = ""
    metadata_update: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class XtbTerminalDependencies:
    write_execution_artifacts: Callable[..., Any]
    selected_xyz: Callable[[Any], Path]
    job_dir: Callable[[Any], Path]
    mark_completed: Callable[..., Any]
    mark_cancelled: Callable[..., Any]
    mark_failed: Callable[..., Any]
    upsert_job_record: Callable[..., Any]
    notify_job_finished: Callable[..., Any]


@dataclass(frozen=True)
class XtbTerminalFinalizationRequest:
    cfg: Any
    queue_root: Path
    entry: Any
    result: XtbRunResult
    emit_output: bool
    outcome_cls: type
    previous_state: dict[str, Any] | None = None
    resumed: bool = False


@dataclass(frozen=True)
class _XtbTerminalPaths:
    job_dir: Path
    selected_xyz: Path


def print_terminal_summary(summary: TerminalSummary) -> None:
    if summary.organized_output_dir:
        print(f"organized_output_dir: {summary.organized_output_dir}")
    print(f"queue_id: {summary.queue_id}")
    print(f"job_id: {summary.job_id}")
    print(f"status: {summary.status}")
    print(f"reason: {summary.reason}")


def terminal_status(
    state: dict[str, Any], report: dict[str, Any], refreshed: Any, rc: int | None
) -> str:
    state = _flatten_engine_payload(state)
    report = _flatten_engine_payload(report)
    queue_status_value = (
        getattr(getattr(refreshed, "status", None), "value", None)
        if refreshed is not None
        else None
    )
    queue_status = str(queue_status_value).strip().lower()
    status = str(report.get("status") or state.get("status") or queue_status).strip().lower()
    if not status:
        return "completed" if rc == 0 else "failed"
    if status not in {"completed", "failed", "cancelled"} and rc is not None:
        return "completed" if rc == 0 else "failed"
    return status


def terminal_reason(
    state: dict[str, Any],
    report: dict[str, Any],
    refreshed: Any,
    *,
    status: str,
    rc: int | None,
) -> str:
    state = _flatten_engine_payload(state)
    report = _flatten_engine_payload(report)
    reason = str(
        report.get("reason") or state.get("reason") or getattr(refreshed, "error", "")
    ).strip()
    if reason:
        return reason
    if status == "completed":
        return "completed"
    if status == "cancelled":
        return "cancel_requested"
    if rc is not None:
        return f"worker_exit_code_{rc}"
    return "unknown"


def terminal_metadata_update(
    state: dict[str, Any], report: dict[str, Any], entry: Any
) -> dict[str, Any]:
    state = _flatten_engine_payload(state)
    report = _flatten_engine_payload(report)
    metadata_update: dict[str, Any] = {}
    job_type = str(
        report.get("job_type") or state.get("job_type") or entry.metadata.get("job_type", "")
    ).strip()
    if job_type:
        metadata_update["job_type"] = job_type
    candidate_count_raw = report.get("candidate_count")
    if candidate_count_raw is None:
        candidate_count_raw = state.get("candidate_count")
    if candidate_count_raw is not None:
        with suppress(TypeError, ValueError):
            metadata_update["candidate_count"] = int(candidate_count_raw)
    return metadata_update


def _flatten_engine_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if int(payload.get("schema_version", 0) or 0) != 1:
        return {}
    status = _mapping(payload.get("status"))
    artifacts = _mapping(payload.get("artifacts"))
    engine_payload = _mapping(payload.get("engine_payload"))
    flattened = dict(engine_payload)
    flattened.setdefault("status", str(status.get("state", "")).strip())
    flattened.setdefault("reason", str(status.get("reason", "")).strip())
    flattened.setdefault("organized_output_dir", str(artifacts.get("organized_dir", "")).strip())
    return flattened


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def load_terminal_summary(
    queue_root: Path,
    entry: Any,
    *,
    rc: int | None = None,
    job_dir_fn: Callable[[Any], Path],
    load_state_fn: Callable[[Path], dict[str, Any] | None],
    load_report_json_fn: Callable[[Path], dict[str, Any] | None],
    load_organized_ref_fn: Callable[[Path], dict[str, Any] | None],
    queue_entry_by_id_fn: Callable[[Path, str], Any | None],
) -> TerminalSummary:
    job_dir = job_dir_fn(entry)
    state = load_state_fn(job_dir) or {}
    report = load_report_json_fn(job_dir) or {}
    organized_ref = load_organized_ref_fn(job_dir) or {}
    refreshed = queue_entry_by_id_fn(queue_root, entry.queue_id)

    status = terminal_status(state, report, refreshed, rc)
    reason = terminal_reason(state, report, refreshed, status=status, rc=rc)
    organized_output_dir = str(
        organized_ref.get("organized_output_dir")
        or _flatten_engine_payload(report).get("organized_output_dir")
        or _flatten_engine_payload(state).get("organized_output_dir")
        or ""
    ).strip()

    return TerminalSummary(
        queue_id=entry.queue_id,
        job_id=entry.task_id,
        status=status,
        reason=reason,
        organized_output_dir=organized_output_dir,
        metadata_update=terminal_metadata_update(state, report, entry),
    )


def ensure_terminal_queue_status(
    queue_root: Path,
    entry: Any,
    summary: TerminalSummary,
    *,
    queue_entry_by_id_fn: Callable[[Path, str], Any | None],
    mark_completed_fn: Callable[..., Any],
    mark_cancelled_fn: Callable[..., Any],
    mark_failed_fn: Callable[..., Any],
) -> None:
    refreshed = queue_entry_by_id_fn(queue_root, entry.queue_id)
    current_status = str(getattr(getattr(refreshed, "status", None), "value", "")).strip().lower()
    if current_status in {"completed", "failed", "cancelled"}:
        return

    metadata_update = summary.metadata_update or None
    _queue_execution.mark_terminal_status(
        queue_root,
        entry.queue_id,
        status=summary.status,
        reason=summary.reason,
        metadata_update=metadata_update,
        mark_completed_fn=mark_completed_fn,
        mark_cancelled_fn=mark_cancelled_fn,
        mark_failed_fn=mark_failed_fn,
    )


def _terminal_paths(
    request: XtbTerminalFinalizationRequest,
    dependencies: XtbTerminalDependencies,
) -> _XtbTerminalPaths:
    job_dir = dependencies.job_dir(request.entry)
    result_selected_xyz = str(request.result.selected_input_xyz).strip()
    selected_xyz = (
        Path(result_selected_xyz).expanduser().resolve()
        if result_selected_xyz
        else dependencies.selected_xyz(request.entry)
    )
    return _XtbTerminalPaths(job_dir=job_dir, selected_xyz=selected_xyz)


def _terminal_metadata(result: XtbRunResult) -> dict[str, Any]:
    return {
        "candidate_count": result.candidate_count,
        "job_type": result.job_type,
    }


def _mark_queue_terminal(
    request: XtbTerminalFinalizationRequest,
    dependencies: XtbTerminalDependencies,
) -> None:
    _engine_execution.mark_result_terminal_status(
        request.queue_root,
        request.entry.queue_id,
        request.result,
        metadata_update=_terminal_metadata(request.result),
        mark_terminal_status_fn=_queue_execution.mark_terminal_status,
        mark_completed_fn=dependencies.mark_completed,
        mark_cancelled_fn=dependencies.mark_cancelled,
        mark_failed_fn=dependencies.mark_failed,
    )


def _sync_job_record(
    request: XtbTerminalFinalizationRequest,
    paths: _XtbTerminalPaths,
    dependencies: XtbTerminalDependencies,
) -> str:
    result = request.result
    entry = request.entry
    dependencies.upsert_job_record(
        request.cfg,
        job_id=entry.task_id,
        status=result.status,
        job_dir=paths.job_dir,
        job_type=result.job_type,
        selected_input_xyz=str(paths.selected_xyz),
        reaction_key=result.reaction_key,
        resource_request=result.resource_request,
        resource_actual=result.resource_actual,
    )
    return ""


def _notify_terminal_finished(
    request: XtbTerminalFinalizationRequest,
    paths: _XtbTerminalPaths,
    dependencies: XtbTerminalDependencies,
    sync_result: str,
) -> None:
    result = request.result
    entry = request.entry
    dependencies.notify_job_finished(
        request.cfg,
        job_id=entry.task_id,
        queue_id=entry.queue_id,
        status=result.status,
        reason=result.reason,
        job_type=result.job_type,
        reaction_key=result.reaction_key,
        job_dir=paths.job_dir,
        selected_xyz=paths.selected_xyz,
        candidate_count=result.candidate_count,
        organized_output_dir=Path(sync_result) if sync_result else None,
        resource_request=result.resource_request,
        resource_actual=result.resource_actual,
    )


def _emit_terminal_summary(request: XtbTerminalFinalizationRequest, sync_result: str) -> None:
    result = request.result
    entry = request.entry
    print_terminal_summary(
        TerminalSummary(
            queue_id=entry.queue_id,
            job_id=entry.task_id,
            status=result.status,
            reason=result.reason,
            organized_output_dir=sync_result,
        )
    )


def _terminal_sync_actions(
    request: XtbTerminalFinalizationRequest,
    paths: _XtbTerminalPaths,
    dependencies: XtbTerminalDependencies,
) -> _engine_execution.TerminalSyncActions:
    return _engine_execution.TerminalSyncActions(
        write_artifacts=lambda: dependencies.write_execution_artifacts(
            request.entry,
            request.result,
            previous_state=request.previous_state,
            resumed=request.resumed,
        ),
        mark_queue_terminal=lambda: _mark_queue_terminal(request, dependencies),
        sync_job_record=lambda: _sync_job_record(request, paths, dependencies),
        notify_finished=lambda sync_result: _notify_terminal_finished(
            request,
            paths,
            dependencies,
            sync_result,
        ),
        emit_output=lambda sync_result: _emit_terminal_summary(request, sync_result),
        build_outcome=lambda sync_result: request.outcome_cls(
            result=request.result,
            organized_output_dir=sync_result,
        ),
    )


def finalize_terminal_result(
    request: XtbTerminalFinalizationRequest,
    *,
    dependencies: XtbTerminalDependencies,
) -> Any:
    paths = _terminal_paths(request, dependencies)
    return _engine_execution.sync_terminal_result(
        _terminal_sync_actions(request, paths, dependencies),
        emit_output=request.emit_output,
    )


def finalize_execution_result(
    cfg: Any,
    *,
    queue_root: Path,
    entry: Any,
    result: XtbRunResult,
    emit_output: bool,
    previous_state: dict[str, Any] | None = None,
    resumed: bool = False,
    outcome_cls: type,
    write_execution_artifacts_fn: Callable[..., Any],
    selected_xyz_fn: Callable[[Any], Path],
    job_dir_fn: Callable[[Any], Path],
    mark_completed_fn: Callable[..., Any],
    mark_cancelled_fn: Callable[..., Any],
    mark_failed_fn: Callable[..., Any],
    upsert_job_record_fn: Callable[..., Any],
    notify_job_finished_fn: Callable[..., Any],
) -> Any:
    return finalize_terminal_result(
        XtbTerminalFinalizationRequest(
            cfg=cfg,
            queue_root=queue_root,
            entry=entry,
            result=result,
            emit_output=emit_output,
            outcome_cls=outcome_cls,
            previous_state=previous_state,
            resumed=resumed,
        ),
        dependencies=XtbTerminalDependencies(
            write_execution_artifacts=write_execution_artifacts_fn,
            selected_xyz=selected_xyz_fn,
            job_dir=job_dir_fn,
            mark_completed=mark_completed_fn,
            mark_cancelled=mark_cancelled_fn,
            mark_failed=mark_failed_fn,
            upsert_job_record=upsert_job_record_fn,
            notify_job_finished=notify_job_finished_fn,
        ),
    )
