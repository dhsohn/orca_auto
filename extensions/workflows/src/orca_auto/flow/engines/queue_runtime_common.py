"""Shared queue-runtime glue for the xtb and crest engines.

Each engine's queue_runtime module keeps its own module-level seams
(config loader, queue markers, notify hooks, terminal repair logic) and
passes late-bound callables into these helpers at call time, so tests keep
monkeypatching the engine module attributes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from orca_auto.core.queue import execution as _queue_execution
from orca_auto.core.queue.engine import artifacts as _engine_artifacts
from orca_auto.core.queue.engine.admission import mark_worker_start_error
from orca_auto.core.statuses import TERMINAL_STATUSES

logger = logging.getLogger(__name__)


def artifact_value(
    state: dict[str, Any],
    key: str,
    default: Any = None,
) -> Any:
    if key in state:
        return state[key]
    state_engine_payload = state.get("engine_payload")
    if isinstance(state_engine_payload, dict) and key in state_engine_payload:
        return state_engine_payload[key]
    return default


def terminal_reason(state: dict[str, Any]) -> str:
    status_payload = state.get("status")
    if isinstance(status_payload, dict):
        reason = str(status_payload.get("reason") or "").strip()
        if reason:
            return reason
    return "terminal_artifact_replay"


def durable_entry_status(entry: Any) -> str:
    return str(getattr(getattr(entry, "status", None), "value", "")).strip().lower()


def resolve_terminal_exit_code(state: dict[str, Any], status: str) -> int | None:
    source_status = state.get("status")
    source_exit_code = source_status.get("exit_code") if isinstance(source_status, dict) else None
    if status == "completed":
        return 0
    matched = (
        isinstance(source_status, dict)
        and str(source_status.get("state") or "").strip().lower() == status
        and type(source_exit_code) is int
    )
    if status == "failed":
        return source_exit_code if matched else 1
    return source_exit_code if matched else None


def resolve_terminal_target(
    entry: Any,
    state: dict[str, Any],
    *,
    durable_status: str,
    record_status: str,
) -> tuple[str, str]:
    target_status = durable_status if durable_status in TERMINAL_STATUSES else record_status
    if target_status == "completed":
        return target_status, "completed"
    if target_status == "cancelled":
        return target_status, "cancel_requested"
    target_reason = str(getattr(entry, "error", "") or terminal_reason(state)).strip()
    return target_status, target_reason or "worker_failed"


def matched_terminal_state_for_adoption(
    queue_root: Path,
    entry: Any,
    *,
    raw_state: dict[str, Any],
    engine: str,
    job_dir: Path,
    mark_completed_fn: Callable[..., Any],
    mark_cancelled_fn: Callable[..., Any],
    mark_failed_fn: Callable[..., Any],
) -> dict[str, Any] | None:
    """Return the identity-matched terminal state, or block the repair.

    A durable terminal queue status that disagrees with (or lacks) a matching
    terminal artifact marks the entry as repair-blocked instead of adopting.
    """
    matched = _engine_artifacts.matching_terminal_state_for_entry(
        state=raw_state,
        entry=entry,
        engine=engine,
        job_dir=job_dir,
    )
    durable_status = durable_entry_status(entry)
    if matched is not None:
        matched_status_payload = matched.get("status")
        matched_status = (
            str(matched_status_payload.get("state") or "").strip().lower()
            if isinstance(matched_status_payload, dict)
            else ""
        )
        if durable_status in TERMINAL_STATUSES and matched_status != durable_status:
            _queue_execution.mark_terminal_repair_blocked(
                queue_root,
                entry,
                durable_status=durable_status,
                mark_completed_fn=mark_completed_fn,
                mark_cancelled_fn=mark_cancelled_fn,
                mark_failed_fn=mark_failed_fn,
            )
            return None
        return matched
    if durable_status in TERMINAL_STATUSES:
        _queue_execution.mark_terminal_repair_blocked(
            queue_root,
            entry,
            durable_status=durable_status,
            mark_completed_fn=mark_completed_fn,
            mark_cancelled_fn=mark_cancelled_fn,
            mark_failed_fn=mark_failed_fn,
        )
    return None


def resolve_adopted_selected_input(
    entry_metadata: dict[str, Any],
    record: Any,
    job_dir: Path,
) -> tuple[Path, Path] | None:
    selected_input = str(entry_metadata.get("selected_input_xyz") or "").strip()
    try:
        selected_input_path = Path(selected_input).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if not selected_input or not selected_input_path.is_relative_to(job_dir):
        return None
    try:
        artifact_selected_input_path = Path(str(record.selected_input_xyz)).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if not artifact_selected_input_path.is_relative_to(job_dir):
        return None
    return selected_input_path, artifact_selected_input_path


def adopt_mark_terminal(
    queue_root: Path,
    entry: Any,
    *,
    target_status: str,
    target_reason: str,
    metadata_update: dict[str, Any] | None,
    persist_terminal_fn: Callable[[str, str], None],
    get_cancel_requested_fn: Callable[..., bool],
    mark_completed_fn: Callable[..., Any],
    mark_cancelled_fn: Callable[..., Any],
    mark_failed_fn: Callable[..., Any],
) -> bool:
    terminal = _queue_execution.mark_terminal_status(
        queue_root,
        entry.queue_id,
        status=target_status,
        reason=target_reason,
        metadata_update=metadata_update,
        mark_completed_fn=mark_completed_fn,
        mark_cancelled_fn=mark_cancelled_fn,
        mark_failed_fn=mark_failed_fn,
        expected_entry=entry,
        expected_task_id=str(entry.task_id),
        before_update_fn=lambda: persist_terminal_fn(target_status, target_reason),
    )
    if terminal is not None:
        return True
    if not get_cancel_requested_fn(
        queue_root,
        entry.queue_id,
        expected_entry=entry,
        expected_task_id=str(entry.task_id),
    ):
        return False

    _queue_execution.mark_terminal_status(
        queue_root,
        entry.queue_id,
        status="cancelled",
        reason="cancel_requested",
        metadata_update=None,
        mark_completed_fn=mark_completed_fn,
        mark_cancelled_fn=mark_cancelled_fn,
        mark_failed_fn=mark_failed_fn,
        expected_entry=entry,
        expected_task_id=str(entry.task_id),
        before_update_fn=lambda: persist_terminal_fn("cancelled", "cancel_requested"),
    )
    return False


def sync_terminal_running_entries(
    worker: Any,
    *,
    is_engine_entry_fn: Callable[[Any], bool],
    queue_entries_with_roots_fn: Callable[[Any], Iterable[tuple[Path, Any]]],
    list_job_records_for_cfg_fn: Callable[[Any], Iterable[tuple[Any, Any]]],
    terminal_entry_needs_repair_fn: Callable[..., bool],
    adopt_terminal_artifacts_fn: Callable[[Any, Path, Any], bool],
    scan_interval_seconds: float,
) -> None:
    monotonic_now = time.monotonic()
    full_terminal_scan = monotonic_now >= float(
        getattr(worker, "_orca_auto_terminal_repair_next_scan", 0.0) or 0.0
    )
    indexed_by_job_id: dict[str, Any] = {}
    if full_terminal_scan:
        try:
            indexed_by_job_id = {
                record.job_id: record for _root, record in list_job_records_for_cfg_fn(worker.cfg)
            }
        except (OSError, RuntimeError, ValueError):
            indexed_by_job_id = {}
    for queue_root, entry in queue_entries_with_roots_fn(worker.cfg):
        if not is_engine_entry_fn(entry):
            continue
        status = durable_entry_status(entry)
        cancelled_marker_needs_repair = status == "cancelled" and (
            bool(getattr(entry, "cancel_requested", False))
            or str(getattr(entry, "error", "") or "").strip() != "cancel_requested"
        )
        if status == "running" or (
            status in TERMINAL_STATUSES
            and (full_terminal_scan or cancelled_marker_needs_repair)
            and terminal_entry_needs_repair_fn(
                worker.cfg,
                entry,
                status=status,
                indexed_record=indexed_by_job_id.get(str(entry.task_id)),
                index_loaded=full_terminal_scan,
            )
        ):
            adopt_terminal_artifacts_fn(worker.cfg, queue_root, entry)
    if full_terminal_scan:
        worker._orca_auto_terminal_repair_next_scan = monotonic_now + scan_interval_seconds


def repair_engine_queue_publications(
    worker: Any,
    *,
    engine: str,
    queue_entries_with_roots_fn: Callable[[Any], Iterable[tuple[Path, Any]]],
    is_engine_entry_fn: Callable[[Any], bool],
    record_queued_fn: Callable[..., Any],
) -> bool:
    from orca_auto.flow.submitters.internal_engine_submission import (
        repair_internal_engine_queue_publication,
    )

    repaired_all = True
    for queue_root, entry in queue_entries_with_roots_fn(worker.cfg):
        if not is_engine_entry_fn(entry):
            continue
        try:
            repaired = repair_internal_engine_queue_publication(
                cfg=worker.cfg,
                queue_root=queue_root,
                entry=entry,
                record_queued_fn=record_queued_fn,
                entry_matches_fn=is_engine_entry_fn,
            )
        except Exception:  # One bad row must not stop the sweep.
            logger.exception(
                "%s queued publication repair raised: queue_id=%s queue_root=%s",
                engine,
                getattr(entry, "queue_id", "unknown"),
                queue_root,
            )
            repaired = False
        if not repaired:
            repaired_all = False
    return repaired_all


def install_publication_repair_gate(
    worker: Any,
    *,
    engine: str,
    repair_fn: Callable[[Any], bool],
) -> None:
    reserve_next_entry: Callable[[], tuple[str, Any | None]] = worker._reserve_next_entry

    def reserve_next_after_publication_repair() -> tuple[str, Any | None]:
        if not repair_fn(worker):
            # Repair is the only path that makes an unpublished row claimable,
            # so a persistent failure here stalls admission silently unless it
            # is reported on every attempt.
            logger.warning(
                "Queue admission paused until %s queued publication repair succeeds",
                engine,
            )
            return "blocked", None
        return reserve_next_entry()

    worker.__dict__["_reserve_next_entry"] = reserve_next_after_publication_repair


def handle_worker_start_error(
    worker: Any,
    queue_root: Path,
    entry: Any,
    admission_token: str,
    exc: OSError,
    *,
    mark_failed_fn: Callable[..., Any],
) -> None:
    mark_worker_start_error(
        queue_root=queue_root,
        entry=entry,
        admission_token=admission_token,
        exc=exc,
        mark_entry_failed_and_release_fn=worker._mark_entry_failed_and_release,
        mark_failed_fn=mark_failed_fn,
    )


__all__ = [
    "adopt_mark_terminal",
    "artifact_value",
    "durable_entry_status",
    "handle_worker_start_error",
    "install_publication_repair_gate",
    "matched_terminal_state_for_adoption",
    "repair_engine_queue_publications",
    "resolve_adopted_selected_input",
    "resolve_terminal_exit_code",
    "resolve_terminal_target",
    "sync_terminal_running_entries",
    "terminal_reason",
]
