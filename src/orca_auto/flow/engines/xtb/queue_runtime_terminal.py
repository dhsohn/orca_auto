from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class XtbQueueRuntimeTerminalCallbacks:
    queue_terminal: Any
    queue_lifecycle: Any
    worker_execution_outcome_cls: type
    job_dir: Callable[[Any], Path]
    selected_xyz: Callable[[Any], Path]
    queue_entry_by_id: Callable[..., Any]
    write_execution_artifacts: Callable[..., Any]
    load_terminal_summary_fn: Callable[..., Any]
    ensure_terminal_queue_status_fn: Callable[..., Any]
    print_terminal_summary_fn: Callable[[Any], Any]
    live_worker_pid_slots_fn: Callable[[Any], list[Any]]
    pid_is_alive: Callable[[int], bool]
    queue_entries_with_roots: Callable[[Any], list[tuple[Path, Any]]]
    list_slots: Callable[[str | Path], list[Any]]
    load_state: Callable[..., Any]
    mark_completed: Callable[..., Any]
    mark_cancelled: Callable[..., Any]
    mark_failed: Callable[..., Any]
    upsert_job_record: Callable[..., Any]
    notify_job_finished: Callable[..., Any]


def load_terminal_summary(
    callbacks: XtbQueueRuntimeTerminalCallbacks,
    queue_root: Path,
    entry: Any,
    *,
    rc: int | None = None,
) -> Any:
    return callbacks.queue_terminal.load_terminal_summary(
        queue_root,
        entry,
        rc=rc,
        job_dir_fn=callbacks.job_dir,
        load_state_fn=callbacks.load_state,
        queue_entry_by_id_fn=callbacks.queue_entry_by_id,
    )


def ensure_terminal_queue_status(
    callbacks: XtbQueueRuntimeTerminalCallbacks,
    queue_root: Path,
    entry: Any,
    summary: Any,
) -> None:
    callbacks.queue_terminal.ensure_terminal_queue_status(
        queue_root,
        entry,
        summary,
        queue_entry_by_id_fn=callbacks.queue_entry_by_id,
        mark_completed_fn=callbacks.mark_completed,
        mark_cancelled_fn=callbacks.mark_cancelled,
        mark_failed_fn=callbacks.mark_failed,
    )


def finalize_execution_result(
    callbacks: XtbQueueRuntimeTerminalCallbacks,
    cfg: Any,
    *,
    queue_root: Path,
    entry: Any,
    result: Any,
    emit_output: bool,
    previous_state: dict[str, Any] | None = None,
    resumed: bool = False,
) -> Any:
    return callbacks.queue_terminal.finalize_execution_result(
        cfg,
        queue_root=queue_root,
        entry=entry,
        result=result,
        emit_output=emit_output,
        previous_state=previous_state,
        resumed=resumed,
        outcome_cls=callbacks.worker_execution_outcome_cls,
        write_execution_artifacts_fn=callbacks.write_execution_artifacts,
        selected_xyz_fn=callbacks.selected_xyz,
        job_dir_fn=callbacks.job_dir,
        mark_completed_fn=callbacks.mark_completed,
        mark_cancelled_fn=callbacks.mark_cancelled,
        mark_failed_fn=callbacks.mark_failed,
        upsert_job_record_fn=callbacks.upsert_job_record,
        notify_job_finished_fn=callbacks.notify_job_finished,
    )


def finalize_completed_job(
    callbacks: XtbQueueRuntimeTerminalCallbacks,
    worker: Any,
    _queue_id: str,
    job: Any,
    rc: int,
) -> None:
    summary = callbacks.load_terminal_summary_fn(job.queue_root, job.entry, rc=rc)
    callbacks.ensure_terminal_queue_status_fn(job.queue_root, job.entry, summary)
    callbacks.print_terminal_summary_fn(summary)
    worker._release_admission_slot(job.admission_token)


def live_worker_pid_slots(
    callbacks: XtbQueueRuntimeTerminalCallbacks,
    worker: Any,
) -> list[Any]:
    return callbacks.queue_lifecycle.live_worker_pid_slots(
        callbacks.queue_entries_with_roots(worker.cfg),
        load_state_fn=callbacks.load_state,
        job_dir_fn=callbacks.job_dir,
        pid_is_alive_fn=callbacks.pid_is_alive,
    )


def list_slots_preserving_live_worker_pids(
    callbacks: XtbQueueRuntimeTerminalCallbacks,
    worker: Any,
    admission_root: str | Path,
) -> list[Any]:
    return [
        *callbacks.list_slots(admission_root),
        *callbacks.live_worker_pid_slots_fn(worker),
    ]


__all__ = [
    "XtbQueueRuntimeTerminalCallbacks",
    "ensure_terminal_queue_status",
    "finalize_completed_job",
    "finalize_execution_result",
    "list_slots_preserving_live_worker_pids",
    "live_worker_pid_slots",
    "load_terminal_summary",
]
