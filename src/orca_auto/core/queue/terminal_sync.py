from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TerminalSyncActions:
    write_artifacts: Callable[[], Any]
    mark_queue_terminal: Callable[[Callable[[], Any]], Any]
    sync_job_record: Callable[[], Any]
    notify_finished: Callable[[Any], Any]
    build_outcome: Callable[[Any], Any]
    emit_output: Callable[[Any], Any] | None = None
    handle_uncommitted_terminal: Callable[[], Any] | None = None


def sync_terminal_result(
    actions: TerminalSyncActions,
    *,
    emit_output: bool = False,
) -> Any:
    # Keep the queue entry replayable until both the terminal artifacts and
    # idempotent terminal index are durable. The queue mutation invokes these
    # writes while holding its generation/cancellation CAS lock, so a stale
    # generation cannot overwrite another job's artifacts and a racing
    # cancellation either wins before any terminal write or waits until the
    # terminal transition is durable.
    sync_results: list[Any] = []

    def sync_before_queue_update() -> Any:
        actions.write_artifacts()
        result = actions.sync_job_record()
        sync_results.append(result)
        return result

    marked = actions.mark_queue_terminal(sync_before_queue_update)
    if marked is None or marked is False:
        if actions.handle_uncommitted_terminal is not None:
            return actions.handle_uncommitted_terminal()
        return actions.build_outcome(None)
    sync_result = sync_results[0] if sync_results else None
    actions.notify_finished(sync_result)
    if emit_output and actions.emit_output is not None:
        actions.emit_output(sync_result)
    return actions.build_outcome(sync_result)


def mark_result_terminal_status(
    queue_root: str | Path,
    queue_id: str,
    result: Any,
    *,
    metadata_update: dict[str, Any] | None,
    mark_terminal_status_fn: Callable[..., Any],
    mark_completed_fn: Callable[..., Any],
    mark_cancelled_fn: Callable[..., Any],
    mark_failed_fn: Callable[..., Any],
    expected_entry: Any | None = None,
    expected_task_id: str | None = None,
    before_update_fn: Callable[[], Any] | None = None,
    require_cancel_requested: bool = False,
) -> Any:
    return mark_terminal_status_fn(
        queue_root,
        queue_id,
        status=result.status,
        reason=result.reason,
        metadata_update=metadata_update,
        mark_completed_fn=mark_completed_fn,
        mark_cancelled_fn=mark_cancelled_fn,
        mark_failed_fn=mark_failed_fn,
        expected_entry=expected_entry,
        expected_task_id=expected_task_id,
        before_update_fn=before_update_fn,
        require_cancel_requested=require_cancel_requested,
    )


def mark_engine_job_running(
    cfg: Any,
    *,
    entry: Any,
    job_dir: Path,
    selected_xyz: Path,
    resource_request: dict[str, int],
    write_running_state_fn: Callable[..., Any],
    upsert_job_record_fn: Callable[..., Any],
    notify_job_started_fn: Callable[..., Any],
    record_fields: dict[str, Any] | None = None,
    notify_fields: dict[str, Any] | None = None,
    write_running_state_kwargs: dict[str, Any] | None = None,
) -> None:
    write_running_state_fn(cfg, entry, **dict(write_running_state_kwargs or {}))
    upsert_job_record_fn(
        cfg,
        job_id=entry.task_id,
        status="running",
        job_dir=job_dir,
        selected_input_xyz=str(selected_xyz),
        **dict(record_fields or {}),
        resource_request=resource_request,
        resource_actual=dict(resource_request),
    )
    notify_job_started_fn(
        cfg,
        job_id=entry.task_id,
        queue_id=entry.queue_id,
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        **dict(notify_fields or {}),
    )


def mark_recovery_pending_and_record(
    cfg: Any,
    *,
    entry: Any,
    job_dir: Path,
    selected_input_xyz: Path | str,
    reason: str,
    resource_request: dict[str, int],
    mark_recovery_pending_fn: Callable[..., Any],
    upsert_job_record_fn: Callable[..., Any],
    state_identity_fields: dict[str, Any] | None = None,
    record_identity_fields: dict[str, Any] | None = None,
) -> None:
    selected_input_xyz_text = str(selected_input_xyz)
    mark_recovery_pending_fn(
        job_dir,
        job_id=str(entry.task_id),
        selected_input_xyz=selected_input_xyz_text,
        **dict(state_identity_fields or {}),
        resource_request=resource_request,
        resource_actual=dict(resource_request),
        reason=reason,
    )
    upsert_job_record_fn(
        cfg,
        job_id=entry.task_id,
        status="pending",
        job_dir=job_dir,
        selected_input_xyz=selected_input_xyz_text,
        **dict(record_identity_fields or {}),
        resource_request=resource_request,
        resource_actual=dict(resource_request),
    )


__all__ = [
    "TerminalSyncActions",
    "mark_engine_job_running",
    "mark_recovery_pending_and_record",
    "mark_result_terminal_status",
    "sync_terminal_result",
]
