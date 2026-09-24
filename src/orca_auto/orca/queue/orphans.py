"""Recovery for ORCA queue entries left running after worker loss."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.admission import AdmissionStore, AdmissionStoreCorruptError
from orca_auto.core.engines import entry_matches_engine_identity
from orca_auto.core.queue import store as _queue_store
from orca_auto.core.queue.child.process import live_queue_slot_keys_for_slots
from orca_auto.core.queue.types import TERMINAL_QUEUE_STATUSES, QueueEntry, QueueStatus
from orca_auto.core.utils.process_tracking import read_pid_file, run_lock_is_held

from ..job_locations._generation import payload_matches_queue_generation
from ..state_reading import load_state
from ..statuses import RunStatus
from .entries import (
    WORKER_PID_FILE_NAME,
    queue_entry_id,
    queue_entry_is_retired_workflow_owned,
    queue_entry_reaction_dir,
    queue_entry_status,
)
from .terminal_replay import (
    terminal_replay_marker_from_entry,
    terminal_replay_metadata_update_fn,
)

logger = logging.getLogger(__name__)


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
    """Orphan variant of the terminal row: the state file, not a worker, decides.

    The row itself comes from ``store.terminal_entry``; only the evidence
    mapping is orphan-specific: the state's ``run_id`` and ``completed_at``
    are authoritative when present, a COMPLETED row drops any stale error, and
    the other statuses keep the recorded error unless the state names one.
    """
    target = QueueStatus(status)
    metadata = dict(entry.metadata)
    if run_id is not None:
        metadata["run_id"] = run_id
    if error is not None:
        updated_error: str | None = error
    elif target == QueueStatus.COMPLETED:
        updated_error = ""
    else:
        updated_error = None
    updated = _queue_store.terminal_entry(
        entry,
        status=target,
        error=updated_error,
        finished_at=finished_at or entry.finished_at or None,
        metadata=metadata,
    )
    if terminal_replay_marker_from_entry(updated) is not None:
        return updated
    marker_update = terminal_replay_metadata_update_fn(
        status=target,
        error=error if error is not None else updated.error,
        allow_terminal_candidate=True,
    )(updated)
    if not marker_update:
        return updated
    return replace(updated, metadata={**updated.metadata, **marker_update})


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
    for entry in entries:
        if entry.status not in TERMINAL_QUEUE_STATUSES:
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
    only_reaction_dirs: frozenset[str] | None = None,
) -> int:
    """Reconcile queue entries stuck as running after worker/process loss.

    ``only_reaction_dirs`` (resolved paths) narrows the pass to rows of those
    directories; every other running row is left untouched.
    """
    if not ignore_worker_pid and read_worker_pid(allowed_root) is not None:
        return 0

    changed = 0
    with _queue_store.queue_lock(allowed_root):
        entries = _queue_store.load_entries(allowed_root)
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
            if only_reaction_dirs is not None and normalized_dir not in only_reaction_dirs:
                continue
            if queue_entry_is_retired_workflow_owned(entry, allowed_root):
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


class DeadRunningRowUnjudgeableError(ValueError):
    """The submitted directory has a dead RUNNING row whose slot protection cannot be read."""


def _has_running_row_for_dir(allowed_root: Path, normalized_dir: str) -> bool:
    with _queue_store.queue_lock(allowed_root):
        entries = _queue_store.load_entries(allowed_root)
    for entry in entries:
        if not entry_matches_engine_identity(entry, "orca"):
            continue
        if queue_entry_status(entry) != QueueStatus.RUNNING.value:
            continue
        reaction_dir = str(queue_entry_reaction_dir(entry) or "").strip()
        if reaction_dir and str(Path(reaction_dir).expanduser().resolve()) == normalized_dir:
            return True
    return False


def reconcile_dead_running_rows_for_dir(
    allowed_root: Path,
    reaction_dir: str,
    *,
    admission_root: Path,
) -> int:
    """Submitter-side fallback: recover *reaction_dir*'s running rows left by a dead worker.

    The worker is the owner of RUNNING-row reconciliation (``replay.reconcile_worker_state``);
    a submission does not sweep the queue. It is allowed to touch only the rows
    of the directory being submitted, and only when every protection the worker
    applies also holds here:

    * no queue worker is alive (``queue_worker.pid``) -- a live worker is already
      responsible for these rows and its child may hold a slot it has not yet
      attached to its row;
    * the row is not tied to an admission slot of a live owner (the worker's
      ``live_queue_slot_keys_for_slots`` predicate; read without normalising the
      admission file, so the submitter never mutates admission state and a slot
      the worker would first have to recover stays protective);
    * ``run.lock`` is not held and the run state belongs to this queue generation
      (``_reconcile_entry``).

    The queue is read first: a directory with no RUNNING row of its own has
    nothing to recover and never touches ``admission_slots.json``, so a damaged
    admission file cannot block a fresh submission. When the directory does
    have a RUNNING row and the admission file cannot be read, the row's
    protection cannot be judged and this submission fails closed with
    ``DeadRunningRowUnjudgeableError`` naming the file.

    Without this pass a directory whose worker died mid-run could not be
    resubmitted until a worker restarted, because its stale RUNNING row rejects
    the new submission as an active duplicate.
    """
    if read_worker_pid(allowed_root) is not None:
        return 0
    normalized_dir = str(Path(reaction_dir).expanduser().resolve())
    if not _has_running_row_for_dir(allowed_root, normalized_dir):
        return 0
    try:
        protected_queue_keys, protected_queue_ids = live_queue_slot_keys_for_slots(
            admission_root,
            list_slots_fn=lambda root: AdmissionStore.for_root(root).list_slots(
                normalize_file=False
            ),
        )
    except AdmissionStoreCorruptError as exc:
        raise DeadRunningRowUnjudgeableError(
            f"{reaction_dir} has a RUNNING queue row left by a dead worker, and whether a "
            f"live slot still protects it cannot be judged: {exc}. Repair or remove "
            f"{AdmissionStore.for_root(admission_root).path} before resubmitting."
        ) from exc
    return reconcile_orphaned_running_entries(
        allowed_root,
        protected_queue_keys=protected_queue_keys,
        protected_queue_ids=protected_queue_ids,
        only_reaction_dirs=frozenset({normalized_dir}),
    )


def _reconcile_entry(
    entry: QueueEntry,
    *,
    prior_terminal_evidence: _PriorTerminalGenerationEvidence | None = None,
) -> QueueEntry | None:
    rdir = queue_entry_reaction_dir(entry)
    if not rdir:
        return None
    reaction_dir = Path(rdir)

    if run_lock_is_held(reaction_dir, logger=logger):
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
        # terminal entry stops advertising a pending cancellation (the
        # CANCELLED row constructor clears it).
        updated = apply_terminal_reconciliation(
            entry,
            status=QueueStatus.CANCELLED.value,
            run_id=None,
            finished_at=None,
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
