from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils.persistence import load_json_mapping_file

from ..statuses import RunStatus


def load_report_payload(
    reaction_dir: Path,
    *,
    report_json_path_fn: Callable[[Path], Path],
    logger: logging.Logger,
) -> dict | None:
    path = report_json_path_fn(reaction_dir)
    raw = load_json_mapping_file(path)
    if raw is None and path.exists():
        logger.warning("Failed to parse run report: %s", path)
        return None
    return raw


def terminal_report_data(
    reaction_dir: Path,
    *,
    load_report_payload_fn: Callable[[Path], dict | None],
) -> tuple[str, str | None, str | None, str | None] | None:
    report = load_report_payload_fn(reaction_dir)
    if report is None:
        return None
    if int(report.get("schema_version", 0) or 0) != 1:
        return None

    status_payload = report.get("status")
    status_dict = status_payload if isinstance(status_payload, dict) else {}
    timestamps = report.get("timestamps")
    timestamps_dict = timestamps if isinstance(timestamps, dict) else {}
    engine_payload = report.get("engine_payload")
    engine_dict = engine_payload if isinstance(engine_payload, dict) else {}
    final_result = engine_dict.get("final_result")
    final_dict = final_result if isinstance(final_result, dict) else {}
    status = str(final_dict.get("status") or status_dict.get("state") or "").strip().lower()
    if status not in {QueueStatus.COMPLETED.value, QueueStatus.FAILED.value}:
        return None

    run_id_text = str(engine_dict.get("run_id", "")).strip()
    finished_at_text = str(
        final_dict.get("completed_at") or timestamps_dict.get("updated_at") or ""
    ).strip()
    error_text = None
    if status == QueueStatus.FAILED.value:
        reason = str(final_dict.get("reason", "")).strip()
        if reason:
            error_text = reason

    return (
        status,
        run_id_text or None,
        finished_at_text or None,
        error_text,
    )


def apply_terminal_reconciliation(
    entry: QueueEntry,
    *,
    status: str,
    run_id: str | None,
    finished_at: str | None,
    error: str | None = None,
    now_iso_fn: Callable[[], str],
) -> QueueEntry:
    metadata = dict(entry.metadata)
    if run_id is not None:
        metadata["run_id"] = run_id
    if error is not None:
        updated_error = error
    elif status == QueueStatus.COMPLETED.value:
        updated_error = ""
    else:
        updated_error = entry.error
    return replace(
        entry,
        status=QueueStatus(status),
        finished_at=finished_at or entry.finished_at or now_iso_fn(),
        error=updated_error,
        metadata=metadata,
    )


def reconcile_orphaned_running_entries(
    allowed_root: Path,
    *,
    ignore_worker_pid: bool = False,
    protected_queue_keys: set[tuple[str, str]] | None = None,
    protected_queue_ids: set[str] | None = None,
    deps: Any,
    logger: logging.Logger,
) -> int:
    """Reconcile queue entries stuck as running after worker/process loss."""
    if not ignore_worker_pid and deps.read_worker_pid(allowed_root) is not None:
        return 0

    changed = 0
    with deps.queue_lock(allowed_root):
        entries = deps.load_entries(allowed_root)
        for index, entry in enumerate(entries):
            if deps.queue_entry_status(entry) != QueueStatus.RUNNING.value:
                continue
            queue_id = str(deps.queue_entry_id(entry) or "").strip()
            reaction_dir = str(deps.queue_entry_reaction_dir(entry) or "").strip()
            normalized_dir = str(Path(reaction_dir).expanduser().resolve()) if reaction_dir else ""
            if queue_id in (protected_queue_ids or set()) or (
                queue_id,
                normalized_dir,
            ) in (protected_queue_keys or set()):
                continue
            updated = _reconcile_entry(entry, deps=deps, logger=logger)
            if updated is None:
                continue
            entries[index] = updated
            changed += 1

        if changed:
            deps.save_entries(allowed_root, entries)
    return changed


def _reconcile_entry(
    entry: QueueEntry,
    *,
    deps: Any,
    logger: logging.Logger,
) -> QueueEntry | None:
    rdir = deps.queue_entry_reaction_dir(entry)
    if not rdir:
        return None
    reaction_dir = Path(rdir)

    if deps.active_lock_pid(reaction_dir) is not None:
        return None

    queue_id = deps.queue_entry_id(entry) or "?"
    state = deps.load_state(reaction_dir)
    run_status = str(state.get("status", "")).strip().lower() if state else ""

    if state is not None and run_status == RunStatus.COMPLETED.value:
        updated = _apply_state_terminal(
            entry,
            state,
            status=QueueStatus.COMPLETED.value,
            default_error=None,
            deps=deps,
        )
        logger.info("Reconciled orphaned entry %s -> completed", queue_id)
        return updated

    if state is not None and run_status == RunStatus.FAILED.value:
        updated = _apply_state_terminal(
            entry,
            state,
            status=QueueStatus.FAILED.value,
            default_error="orphaned_worker_crash",
            deps=deps,
        )
        logger.info("Reconciled orphaned entry %s -> failed", queue_id)
        return updated

    report_data = deps.terminal_report_data(reaction_dir)
    if report_data is not None:
        status, run_id, finished_at, error = report_data
        updated = deps.apply_terminal_reconciliation(
            entry,
            status=status,
            run_id=run_id,
            finished_at=finished_at,
            error=error,
        )
        logger.info("Reconciled orphaned entry %s -> %s (from job_report)", queue_id, status)
        return updated

    if entry.cancel_requested:
        # A cancel was requested while this entry was running, then the worker
        # process was lost before it could honor it. Re-queueing to PENDING would
        # strand the entry forever: dequeue skips cancel_requested entries, so it
        # would never be re-run, and no path transitions a PENDING+cancel_requested
        # entry to a terminal state. Honor the cancellation instead, mirroring
        # store.requeue_running_entry's cancel chokepoint, and clear the flag so the
        # terminal entry stops advertising a pending cancellation.
        updated = replace(
            deps.apply_terminal_reconciliation(
                entry,
                status=QueueStatus.CANCELLED.value,
                run_id=None,
                finished_at=None,
            ),
            cancel_requested=False,
        )
        logger.info("Reconciled orphaned entry %s -> cancelled (cancel_requested)", queue_id)
        return updated

    updated = replace(entry, status=QueueStatus.PENDING, started_at="")
    logger.info("Reconciled orphaned entry %s -> pending (re-queue)", queue_id)
    return updated


def _apply_state_terminal(
    entry: QueueEntry,
    state: dict[str, Any],
    *,
    status: str,
    default_error: str | None,
    deps: Any,
) -> QueueEntry:
    final_result = state.get("final_result")
    final_dict = final_result if isinstance(final_result, dict) else {}
    error = None
    if default_error is not None:
        error = str(final_dict.get("reason", "")).strip() or default_error
    return deps.apply_terminal_reconciliation(
        entry,
        status=status,
        run_id=str(state.get("run_id", "")).strip() or None,
        finished_at=str(final_dict.get("completed_at") or state.get("updated_at") or "").strip()
        or None,
        error=error,
    )
