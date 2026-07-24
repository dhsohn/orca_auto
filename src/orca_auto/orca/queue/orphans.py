"""Recovery for ORCA queue entries left running after worker loss."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.engines import entry_matches_engine_identity
from orca_auto.core.queue import store as _queue_store
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils.persistence import now_utc_iso
from orca_auto.core.utils.process_tracking import active_run_lock_pid, read_pid_file

from ..job_locations._generation import payload_matches_queue_generation
from ..state import load_state
from ..statuses import RunStatus
from .entries import (
    WORKER_PID_FILE_NAME,
    queue_entry_id,
    queue_entry_reaction_dir,
    queue_entry_status,
)
from .entries import (
    load_entries as _load_entries,
)
from .terminal_replay import (
    TERMINAL_REPLAY_METADATA_KEY,
    terminal_replay_marker_for_entry,
    terminal_replay_marker_from_entry,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return now_utc_iso()


def active_lock_pid(reaction_dir: Path) -> int | None:
    return active_run_lock_pid(
        reaction_dir,
        on_pid_reuse=lambda pid, expected_ticks, observed_ticks: logger.info(
            "Ignoring stale run.lock due to PID reuse: reaction_dir=%s pid=%d expected=%d observed=%s",
            reaction_dir,
            pid,
            expected_ticks,
            observed_ticks,
        ),
    )


def read_worker_pid(allowed_root: Path) -> int | None:
    return read_pid_file(allowed_root / WORKER_PID_FILE_NAME)


def apply_terminal_reconciliation(
    entry: QueueEntry,
    *,
    status: str,
    run_id: str | None,
    finished_at: str | None,
    error: str | None = None,
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
    updated = replace(
        entry,
        status=QueueStatus(status),
        finished_at=finished_at or entry.finished_at or _now_iso(),
        error=updated_error,
        metadata=metadata,
    )
    if terminal_replay_marker_from_entry(updated) is not None:
        return updated
    metadata = dict(updated.metadata)
    metadata[TERMINAL_REPLAY_METADATA_KEY] = terminal_replay_marker_for_entry(
        updated,
        status=status,
        error=error if error is not None else updated.error,
    )
    return replace(updated, metadata=metadata)


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


def reconcile_orphaned_running_entries(
    allowed_root: Path,
    *,
    ignore_worker_pid: bool = False,
    protected_queue_keys: set[tuple[str, str]] | None = None,
    protected_queue_ids: set[str] | None = None,
) -> int:
    """Reconcile queue entries stuck as running after worker/process loss."""
    if not ignore_worker_pid and read_worker_pid(allowed_root) is not None:
        return 0

    changed = 0
    with _queue_store.queue_lock(allowed_root):
        entries = _load_entries(allowed_root)
        owned_entries = [entry for entry in entries if entry_matches_engine_identity(entry, "orca")]
        prior_evidence_by_key = _prior_terminal_generation_evidence(owned_entries)
        for index, entry in enumerate(entries):
            if not entry_matches_engine_identity(entry, "orca"):
                continue
            if queue_entry_status(entry) != QueueStatus.RUNNING.value:
                continue
            queue_id = str(queue_entry_id(entry) or "").strip()
            reaction_dir = str(queue_entry_reaction_dir(entry) or "").strip()
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
                prior_terminal_evidence=prior_terminal_evidence,
            )
            if updated is None:
                continue
            entries[index] = updated
            changed += 1

        if changed:
            _queue_store.save_entries(allowed_root, entries)
    return changed


def _reconcile_entry(
    entry: QueueEntry,
    *,
    prior_terminal_evidence: _PriorTerminalGenerationEvidence | None = None,
) -> QueueEntry | None:
    rdir = queue_entry_reaction_dir(entry)
    if not rdir:
        return None
    reaction_dir = Path(rdir)

    if active_lock_pid(reaction_dir) is not None:
        return None

    queue_id = queue_entry_id(entry) or "?"
    loaded_state = load_state(reaction_dir)
    state: dict[str, Any] | None = dict(loaded_state) if loaded_state is not None else None
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
        )
        logger.info("Reconciled orphaned entry %s -> completed", queue_id)
        return updated

    if state is not None and run_status == RunStatus.FAILED.value:
        updated = _apply_state_terminal(
            entry,
            state,
            status=QueueStatus.FAILED.value,
            default_error="orphaned_worker_crash",
        )
        logger.info("Reconciled orphaned entry %s -> failed", queue_id)
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
            apply_terminal_reconciliation(
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
) -> QueueEntry:
    final_result = state.get("final_result")
    final_dict = final_result if isinstance(final_result, dict) else {}
    error = None
    if default_error is not None:
        error = str(final_dict.get("reason", "")).strip() or default_error
    return apply_terminal_reconciliation(
        entry,
        status=status,
        run_id=str(state.get("run_id", "")).strip() or None,
        finished_at=str(final_dict.get("completed_at") or state.get("updated_at") or "").strip()
        or None,
        error=error,
    )
