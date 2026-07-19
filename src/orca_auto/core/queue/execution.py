from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.utils import mapping_or_empty

from .processes import managed_process_group_has_exited


def coerce_mapping(value: Any) -> dict[str, Any]:
    return dict(mapping_or_empty(value))


def recovery_reason(state: dict[str, Any] | None) -> str:
    base_state = coerce_mapping(state)
    recovery = coerce_mapping(base_state.get("recovery"))
    status = coerce_mapping(base_state.get("status"))
    return str(
        recovery.get("reason")
        or status.get("reason")
        or base_state.get("recovery_reason")
        or base_state.get("reason")
        or ""
    ).strip()


def recovery_count(state: dict[str, Any] | None) -> int:
    base_state = coerce_mapping(state)
    recovery = coerce_mapping(base_state.get("recovery"))
    return int(recovery.get("count", base_state.get("recovery_count", 0)) or 0)


def created_at(state: dict[str, Any] | None) -> str:
    base_state = coerce_mapping(state)
    timestamps = coerce_mapping(base_state.get("timestamps"))
    return str(timestamps.get("created_at") or base_state.get("created_at", "")).strip()


def load_matching_state(
    job_dir: Path,
    *,
    load_state_fn: Callable[[Path], dict[str, Any] | None],
    state_matches_job_fn: Callable[..., bool],
    match_kwargs: dict[str, Any],
) -> dict[str, Any]:
    state = load_state_fn(job_dir) or {}
    if state_matches_job_fn(state, **match_kwargs):
        return state
    return {}


def mark_terminal_status(
    queue_root: str | Path,
    queue_id: str,
    *,
    status: str,
    reason: str,
    metadata_update: dict[str, Any] | None,
    mark_completed_fn: Callable[..., Any],
    mark_cancelled_fn: Callable[..., Any],
    mark_failed_fn: Callable[..., Any],
    expected_entry: Any | None = None,
    expected_task_id: str | None = None,
    before_update_fn: Callable[[], Any] | None = None,
    require_cancel_requested: bool = False,
) -> Any:
    generation_kwargs = {}
    if expected_entry is not None:
        generation_kwargs["expected_entry"] = expected_entry
    if expected_task_id:
        generation_kwargs["expected_task_id"] = expected_task_id
    if before_update_fn is not None:
        generation_kwargs["before_update_fn"] = before_update_fn
    if require_cancel_requested:
        generation_kwargs["require_cancel_requested"] = True
    if status == "completed":
        return mark_completed_fn(
            str(queue_root),
            queue_id,
            metadata_update=metadata_update,
            **generation_kwargs,
        )
    if status == "cancelled":
        return mark_cancelled_fn(
            str(queue_root),
            queue_id,
            error=reason,
            metadata_update=metadata_update,
            **generation_kwargs,
        )
    return mark_failed_fn(
        str(queue_root),
        queue_id,
        error=reason,
        metadata_update=metadata_update,
        **generation_kwargs,
    )


def mark_terminal_repair_blocked(
    queue_root: str | Path,
    entry: Any,
    *,
    durable_status: str,
    mark_completed_fn: Callable[..., Any],
    mark_cancelled_fn: Callable[..., Any],
    mark_failed_fn: Callable[..., Any],
    blocked_reason: str = "terminal_state_unrecoverable",
) -> Any:
    normalized_status = str(durable_status).strip().lower()
    if normalized_status == "completed":
        reason = "completed"
    elif normalized_status == "cancelled":
        reason = "cancel_requested"
    elif normalized_status == "failed":
        reason = str(getattr(entry, "error", "") or blocked_reason).strip()
    else:
        raise ValueError(f"Terminal repair blocker requires terminal status: {durable_status}")
    return mark_terminal_status(
        queue_root,
        entry.queue_id,
        status=normalized_status,
        reason=reason,
        metadata_update={"terminal_repair_blocked_reason": blocked_reason},
        mark_completed_fn=mark_completed_fn,
        mark_cancelled_fn=mark_cancelled_fn,
        mark_failed_fn=mark_failed_fn,
        expected_entry=entry,
        expected_task_id=str(entry.task_id),
    )


def wait_for_cancellable_process(
    running: Any,
    *,
    finalize_fn: Callable[..., Any],
    terminate_process_fn: Callable[[Any], Any],
    should_cancel: Callable[[], bool] | None = None,
    shutdown_requested: Callable[[], bool] | None = None,
    on_shutdown: Callable[[Any], Any] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_interval_seconds: float = 1.0,
    check_cancel_before_poll: bool = False,
) -> Any:
    process = running.process

    def finish_cancelled() -> Any:
        terminate_process_fn(process)
        return finalize_fn(
            running,
            forced_status="cancelled",
            forced_reason="cancel_requested",
        )

    while True:
        if check_cancel_before_poll and should_cancel is not None and should_cancel():
            return finish_cancelled()

        if process.poll() is not None:
            if not managed_process_group_has_exited(process):
                terminate_process_fn(process)
            return finalize_fn(running)

        if not check_cancel_before_poll and should_cancel is not None and should_cancel():
            return finish_cancelled()

        if shutdown_requested is not None and shutdown_requested():
            terminate_process_fn(process)
            if on_shutdown is not None:
                return on_shutdown(running)
            return finalize_fn(
                running,
                forced_status="cancelled",
                forced_reason="worker_shutdown",
            )

        sleep_fn(poll_interval_seconds)
