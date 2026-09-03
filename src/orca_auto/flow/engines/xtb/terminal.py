from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Generic, TypeVar

from orca_auto.core.queue import execution as _queue_execution
from orca_auto.core.queue.engine import execution as _engine_execution
from orca_auto.core.utils import copy_dict_or_empty as _mapping
from orca_auto.flow.engines.xtb.runner import XtbRunResult

OutcomeT = TypeVar("OutcomeT")


@dataclass(frozen=True)
class TerminalSummary:
    queue_id: str
    job_id: str
    status: str
    reason: str
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
class XtbTerminalFinalizationRequest(Generic[OutcomeT]):
    cfg: Any
    queue_root: Path
    entry: Any
    result: XtbRunResult
    emit_output: bool
    outcome_cls: Callable[..., OutcomeT]
    previous_state: dict[str, Any] | None = None
    resumed: bool = False


@dataclass(frozen=True)
class _XtbTerminalPaths:
    job_dir: Path
    selected_xyz: Path


def print_terminal_summary(summary: TerminalSummary) -> None:
    print(f"queue_id: {summary.queue_id}")
    print(f"job_id: {summary.job_id}")
    print(f"status: {summary.status}")
    print(f"reason: {summary.reason}")


def terminal_status(state: dict[str, Any], refreshed: Any, rc: int | None) -> str:
    state = _flatten_engine_payload(state)
    queue_status_value = (
        getattr(getattr(refreshed, "status", None), "value", None)
        if refreshed is not None
        else None
    )
    queue_status = str(queue_status_value).strip().lower()
    status = str(state.get("status") or queue_status).strip().lower()
    if not status:
        return "completed" if rc == 0 else "failed"
    if status not in {"completed", "failed", "cancelled"} and rc is not None:
        return "completed" if rc == 0 else "failed"
    return status


def terminal_reason(
    state: dict[str, Any],
    refreshed: Any,
    *,
    status: str,
    rc: int | None,
) -> str:
    state = _flatten_engine_payload(state)
    reason = str(state.get("reason") or getattr(refreshed, "error", "")).strip()
    if reason:
        return reason
    if status == "completed":
        return "completed"
    if status == "cancelled":
        return "cancel_requested"
    if rc is not None:
        return f"worker_exit_code_{rc}"
    return "unknown"


def terminal_metadata_update(state: dict[str, Any]) -> dict[str, Any]:
    state = _flatten_engine_payload(state)
    metadata_update: dict[str, Any] = {}
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
    engine_payload = _mapping(payload.get("engine_payload"))
    flattened = dict(engine_payload)
    flattened.setdefault("status", str(status.get("state", "")).strip())
    flattened.setdefault("reason", str(status.get("reason", "")).strip())
    return flattened


def load_terminal_summary(
    queue_root: Path,
    entry: Any,
    *,
    rc: int | None = None,
    job_dir_fn: Callable[[Any], Path],
    load_state_fn: Callable[[Path], dict[str, Any] | None],
    queue_entry_by_id_fn: Callable[[Path, str], Any | None],
) -> TerminalSummary:
    job_dir = job_dir_fn(entry)
    state = load_state_fn(job_dir) or {}
    refreshed = queue_entry_by_id_fn(queue_root, entry.queue_id)

    status = terminal_status(state, refreshed, rc)
    reason = terminal_reason(state, refreshed, status=status, rc=rc)
    return TerminalSummary(
        queue_id=entry.queue_id,
        job_id=entry.task_id,
        status=status,
        reason=reason,
        metadata_update=terminal_metadata_update(state),
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
) -> bool:
    """Mark a still-running row with the child's terminal outcome.

    Returns whether the row is terminal afterwards.  A row that is no longer
    running is left alone: a child that requeued itself for recovery (worker
    shutdown) exits 0 with the row back at ``pending`` and the run state
    marked recovery-pending, and marking that row completed would close a
    generation that has not run.  CREST already leaves such a row pending.
    """
    refreshed = queue_entry_by_id_fn(queue_root, entry.queue_id)
    current_status = str(getattr(getattr(refreshed, "status", None), "value", "")).strip().lower()
    if current_status in {"completed", "failed", "cancelled"}:
        return True
    if current_status != "running":
        return False

    metadata_update = summary.metadata_update or None
    marked = _queue_execution.mark_terminal_status(
        queue_root,
        entry.queue_id,
        status=summary.status,
        reason=summary.reason,
        metadata_update=metadata_update,
        mark_completed_fn=mark_completed_fn,
        mark_cancelled_fn=mark_cancelled_fn,
        mark_failed_fn=mark_failed_fn,
        expected_entry=entry,
        expected_task_id=str(getattr(entry, "task_id", "") or "") or None,
    )
    # The store refuses a mark for another generation or for a row whose
    # cancel request outranks this outcome; that row is not terminal yet.
    return marked is not None


def _terminal_paths(
    request: XtbTerminalFinalizationRequest[Any],
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
    }


def _mark_queue_terminal(
    request: XtbTerminalFinalizationRequest[Any],
    dependencies: XtbTerminalDependencies,
    before_update_fn: Callable[[], Any] | None = None,
    require_cancel_requested: bool = False,
) -> Any:
    return _engine_execution.mark_result_terminal_status(
        request.queue_root,
        request.entry.queue_id,
        request.result,
        metadata_update=_terminal_metadata(request.result),
        mark_terminal_status_fn=_queue_execution.mark_terminal_status,
        mark_completed_fn=dependencies.mark_completed,
        mark_cancelled_fn=dependencies.mark_cancelled,
        mark_failed_fn=dependencies.mark_failed,
        expected_entry=request.entry,
        expected_task_id=str(request.entry.task_id),
        before_update_fn=before_update_fn,
        require_cancel_requested=require_cancel_requested,
    )


def _sync_job_record(
    request: XtbTerminalFinalizationRequest[Any],
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
    request: XtbTerminalFinalizationRequest[Any],
    paths: _XtbTerminalPaths,
    dependencies: XtbTerminalDependencies,
    sync_result: str | None,
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
        resource_request=result.resource_request,
        resource_actual=result.resource_actual,
    )


def _emit_terminal_summary(
    request: XtbTerminalFinalizationRequest[Any],
    sync_result: str | None,
) -> None:
    result = request.result
    entry = request.entry
    print_terminal_summary(
        TerminalSummary(
            queue_id=entry.queue_id,
            job_id=entry.task_id,
            status=result.status,
            reason=result.reason,
        )
    )


def _terminal_sync_actions(
    request: XtbTerminalFinalizationRequest[OutcomeT],
    paths: _XtbTerminalPaths,
    dependencies: XtbTerminalDependencies,
) -> _engine_execution.TerminalSyncActions[str, OutcomeT]:
    handle_uncommitted_terminal: Callable[[], OutcomeT] | None = None
    if request.result.status != "cancelled":

        def handle_uncommitted_terminal() -> OutcomeT:
            cancelled_request = replace(
                request,
                result=replace(
                    request.result,
                    status="cancelled",
                    reason="cancel_requested",
                ),
            )
            return finalize_terminal_result(
                cancelled_request,
                dependencies=dependencies,
                require_cancel_requested=True,
                uncommitted_result=request.result,
            )

    return _engine_execution.TerminalSyncActions(
        write_artifacts=lambda: dependencies.write_execution_artifacts(
            request.entry,
            request.result,
            previous_state=request.previous_state,
            resumed=request.resumed,
        ),
        mark_queue_terminal=lambda before_update_fn: _mark_queue_terminal(
            request,
            dependencies,
            before_update_fn,
        ),
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
        ),
        handle_uncommitted_terminal=handle_uncommitted_terminal,
    )


def finalize_terminal_result(
    request: XtbTerminalFinalizationRequest[OutcomeT],
    *,
    dependencies: XtbTerminalDependencies,
    require_cancel_requested: bool = False,
    uncommitted_result: XtbRunResult | None = None,
) -> OutcomeT:
    paths = _terminal_paths(request, dependencies)
    actions = _terminal_sync_actions(request, paths, dependencies)
    if require_cancel_requested:
        actions = replace(
            actions,
            mark_queue_terminal=lambda before_update_fn: _mark_queue_terminal(
                request,
                dependencies,
                before_update_fn,
                require_cancel_requested=True,
            ),
            handle_uncommitted_terminal=(
                (lambda: request.outcome_cls(result=uncommitted_result))
                if uncommitted_result is not None
                else None
            ),
        )
    return _engine_execution.sync_terminal_result(
        actions,
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
    outcome_cls: Callable[..., OutcomeT],
    write_execution_artifacts_fn: Callable[..., Any],
    selected_xyz_fn: Callable[[Any], Path],
    job_dir_fn: Callable[[Any], Path],
    mark_completed_fn: Callable[..., Any],
    mark_cancelled_fn: Callable[..., Any],
    mark_failed_fn: Callable[..., Any],
    upsert_job_record_fn: Callable[..., Any],
    notify_job_finished_fn: Callable[..., Any],
) -> OutcomeT:
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
