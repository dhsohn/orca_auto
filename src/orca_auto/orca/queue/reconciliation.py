from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.engines import entry_matches_engine_identity
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils.persistence import load_json_mapping_file

from ..job_locations._generation import payload_matches_queue_generation
from ..statuses import RunStatus


@dataclass(frozen=True)
class _PriorTerminalGenerationEvidence:
    run_ids: frozenset[str]
    has_unidentified_run: bool = False


def _queue_generation_payload(entry: QueueEntry) -> dict[str, Any]:
    return {
        "task_id": str(entry.task_id or "").strip(),
        "metadata": dict(entry.metadata) if isinstance(entry.metadata, dict) else {},
    }


def payload_matches_entry_generation(entry: QueueEntry, payload: dict[str, Any]) -> bool:
    return payload_matches_queue_generation(_queue_generation_payload(entry), payload)


def _payload_run_id(payload: dict[str, Any]) -> str:
    engine_payload = payload.get("engine_payload")
    engine = engine_payload if isinstance(engine_payload, dict) else {}
    return str(payload.get("run_id") or engine.get("run_id") or "").strip()


def _entry_generation_key(entry: QueueEntry) -> tuple[str, str] | None:
    reaction_dir = str(entry.metadata.get("reaction_dir") or "").strip()
    task_id = str(entry.task_id or "").strip()
    if not reaction_dir or not task_id:
        return None
    return (str(Path(reaction_dir).expanduser().resolve()), task_id)


def _prior_terminal_generation_evidence(
    entries: list[QueueEntry],
) -> dict[tuple[str, str], _PriorTerminalGenerationEvidence]:
    run_ids_by_key: dict[tuple[str, str], set[str]] = {}
    unidentified_keys: set[tuple[str, str]] = set()
    terminal_statuses = {
        QueueStatus.COMPLETED,
        QueueStatus.FAILED,
        QueueStatus.CANCELLED,
    }
    for entry in entries:
        if entry.status not in terminal_statuses:
            continue
        key = _entry_generation_key(entry)
        if key is None:
            continue
        run_id = str(entry.metadata.get("run_id") or "").strip()
        if run_id:
            run_ids_by_key.setdefault(key, set()).add(run_id)
        else:
            unidentified_keys.add(key)
    return {
        key: _PriorTerminalGenerationEvidence(
            run_ids=frozenset(run_ids_by_key.get(key, set())),
            has_unidentified_run=key in unidentified_keys,
        )
        for key in run_ids_by_key.keys() | unidentified_keys
    }


def _artifact_is_known_prior_generation(
    payload: dict[str, Any],
    evidence: _PriorTerminalGenerationEvidence | None,
) -> bool:
    if evidence is None:
        return False
    run_id = _payload_run_id(payload)
    return evidence.has_unidentified_run or not run_id or run_id in evidence.run_ids


def _run_id_is_known_prior_generation(
    run_id: str | None,
    evidence: _PriorTerminalGenerationEvidence | None,
) -> bool:
    if evidence is None:
        return False
    normalized = str(run_id or "").strip()
    return evidence.has_unidentified_run or not normalized or normalized in evidence.run_ids


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
    queue_entry: QueueEntry | None = None,
) -> tuple[str, str | None, str | None, str | None] | None:
    report = load_report_payload_fn(reaction_dir)
    if report is None:
        return None
    if queue_entry is not None and not payload_matches_entry_generation(queue_entry, report):
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
        owned_entries = [entry for entry in entries if entry_matches_engine_identity(entry, "orca")]
        prior_evidence_by_key = _prior_terminal_generation_evidence(owned_entries)
        for index, entry in enumerate(entries):
            if not entry_matches_engine_identity(entry, "orca"):
                continue
            if deps.queue_entry_status(entry) != QueueStatus.RUNNING.value:
                continue
            queue_id = str(deps.queue_entry_id(entry) or "").strip()
            reaction_dir = str(deps.queue_entry_reaction_dir(entry) or "").strip()
            normalized_dir = str(Path(reaction_dir).expanduser().resolve()) if reaction_dir else ""
            if not normalized_dir or not Path(normalized_dir).is_relative_to(
                allowed_root.expanduser().resolve()
            ):
                continue
            if queue_id in (protected_queue_ids or set()) or (
                queue_id,
                normalized_dir,
            ) in (protected_queue_keys or set()):
                continue
            metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
            entry_key = _entry_generation_key(entry)
            prior_terminal_evidence = (
                prior_evidence_by_key.get(entry_key)
                if bool(metadata.get("force")) and entry_key is not None
                else None
            )
            updated = _reconcile_entry(
                entry,
                deps=deps,
                logger=logger,
                prior_terminal_evidence=prior_terminal_evidence,
            )
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
    prior_terminal_evidence: _PriorTerminalGenerationEvidence | None = None,
) -> QueueEntry | None:
    rdir = deps.queue_entry_reaction_dir(entry)
    if not rdir:
        return None
    reaction_dir = Path(rdir)

    if deps.active_lock_pid(reaction_dir) is not None:
        return None

    queue_id = deps.queue_entry_id(entry) or "?"
    state = deps.load_state(reaction_dir)
    if state is not None and (
        not payload_matches_entry_generation(entry, state)
        or _artifact_is_known_prior_generation(state, prior_terminal_evidence)
    ):
        state = None
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

    report_data = deps.terminal_report_data(reaction_dir, queue_entry=entry)
    if report_data is not None and _run_id_is_known_prior_generation(
        report_data[1],
        prior_terminal_evidence,
    ):
        report_data = None
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
