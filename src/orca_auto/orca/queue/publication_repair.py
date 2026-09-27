"""Worker-side repair of the queued-record publication lease.

Before a row can be claimed, the worker's repair pass validates its bound
paths and, for a lease still in flight, re-claims it with a fresh REPAIRING
token under the publication lock, publishes the queued job record and
completes the lease. The lease protocol is described in
:mod:`orca_auto.core.queue.publication`.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path

from orca_auto.core.paths import should_exclude_from_production_runs_scan
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_BLOCKED_KEY,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_REPAIRING,
    REPAIRABLE_SYNC_STATES,
    park_queue_record_repair_pending,
    queue_record_publication_lock,
    queue_record_sync_metadata,
    queue_record_sync_state,
    queue_record_sync_token,
)
from orca_auto.core.queue.store import mutate_entries
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.statuses import STATUS_QUEUED
from orca_auto.core.utils.lock import FileLockTimeoutError
from orca_auto.core.utils.persistence import timestamped_token

from ..config import AppConfig
from . import roots
from .adapter import get_entry_by_id, list_queue, mark_failed
from .entries import is_orca_queue_entry, queue_entry_id, queue_entry_reaction_dir, same_generation
from .job_records import upsert_row_job_record

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepairOutcome:
    """One repair attempt's result; ``reason`` names the branch taken.

    ``published`` means this call claimed the lease and published the queued
    record. ``complete``/``cancelled``/``running``/``terminal``/``missing``
    mean there was nothing left for this repair to do. ``invalid_state`` and
    ``identity_changed`` are refusals; ``failed``/``claim_failed`` mean the
    attempt raised (``error`` carries the exception) and the lease was parked
    back to REPAIR_PENDING.
    """

    reason: str
    entry: QueueEntry | None = None
    error: BaseException | None = None

    @property
    def repaired(self) -> bool:
        return self.reason in {
            "published",
            "complete",
            "cancelled",
            "running",
            "terminal",
            "missing",
        }


def _claim_repair_lease(
    entries: list[QueueEntry],
    entry: QueueEntry,
    *,
    repair_token: str,
) -> tuple[tuple[str, QueueEntry | None], bool]:
    """Claim a repairable row with a fresh REPAIRING lease, or name why not.

    The first element of the result is the :class:`RepairOutcome` reason
    (``claimed`` when the lease was taken) and the row it was decided on.
    """

    for index, current in enumerate(entries):
        if current.queue_id != entry.queue_id:
            continue
        if not same_generation(current, entry):
            return ("identity_changed", current), False
        if current.cancel_requested:
            return ("cancelled", current), False
        sync_state = queue_record_sync_state(current)
        if sync_state == QUEUE_RECORD_SYNC_COMPLETE:
            return ("complete", current), False
        if current.status == QueueStatus.RUNNING:
            return ("running", current), False
        if current.status != QueueStatus.PENDING:
            return ("terminal", current), False
        if sync_state not in REPAIRABLE_SYNC_STATES:
            return ("invalid_state", current), False
        metadata = dict(current.metadata)
        metadata.update(
            queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_REPAIRING,
                token=repair_token,
                owner_pid=os.getpid(),
            )
        )
        updated = replace(current, metadata=metadata)
        entries[index] = updated
        return ("claimed", updated), True
    return ("missing", None), False


def _complete_repair_lease(
    entries: list[QueueEntry],
    entry: QueueEntry,
    *,
    repair_token: str,
) -> tuple[QueueEntry | None, bool]:
    """CAS this repair's REPAIRING lease to COMPLETE; anything else is a lost lease."""

    for index, row in enumerate(entries):
        if row.queue_id != entry.queue_id:
            continue
        # The COMPLETE transition belongs to this repair lease only: any
        # other sync state or token here (for example a cancel fence
        # written concurrently) must never be overwritten.
        if (
            row.status != QueueStatus.PENDING
            or row.cancel_requested
            or queue_record_sync_state(row) != QUEUE_RECORD_SYNC_REPAIRING
            or queue_record_sync_token(row) != repair_token
        ):
            raise RuntimeError(
                f"ORCA: queued record repair lost publication ownership: queue_id={entry.queue_id}"
            )
        metadata = dict(row.metadata)
        metadata.update(
            queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_COMPLETE,
                token=repair_token,
                owner_pid=0,
            )
        )
        updated = replace(row, metadata=metadata)
        entries[index] = updated
        return updated, True
    raise RuntimeError(f"ORCA: queue entry disappeared during repair: {entry.queue_id}")


def _log_repair_refusal(entry: QueueEntry, *, reason: str, current: QueueEntry | None) -> None:
    """Log the two refusals; the other non-claim reasons leave nothing to do."""

    if reason == "invalid_state":
        logger.error(
            "ORCA: cannot repair queue publication with invalid state %r: queue_id=%s",
            queue_record_sync_state(current),
            entry.queue_id,
        )
    elif reason == "identity_changed":
        logger.warning(
            "ORCA: queued record repair refused a changed queue generation: queue_id=%s",
            entry.queue_id,
        )
    # complete / cancelled / running / terminal / missing: nothing
    # left for this repair to do.


def _park_failed_repair_lease(queue_root: Path, entry: QueueEntry, *, repair_token: str) -> None:
    """Park this repair's lease REPAIR_PENDING after a failure, never masking it.

    Even a failed claim may have committed before its durability barrier
    reported failure; the park CAS is token-gated, so it never touches a row
    this repair does not own.
    """

    def park_repair_lease(entries: list[QueueEntry]) -> tuple[None, bool]:
        return park_queue_record_repair_pending(
            entries,
            entry,
            expected_state=QUEUE_RECORD_SYNC_REPAIRING,
            expected_token=repair_token,
        )

    try:
        mutate_entries(queue_root, park_repair_lease)
    except BaseException:  # noqa: BLE001 - never mask the original failure
        logger.warning(
            "ORCA: failed to park queued record as repair pending: queue_id=%s",
            getattr(entry, "queue_id", ""),
            exc_info=True,
        )


def repair_enqueue_publication_outcome(
    cfg: AppConfig,
    queue_root: Path,
    entry: QueueEntry,
) -> RepairOutcome:
    """Re-publish one committed row whose queued record never landed.

    Claims the row under the publication lock with a fresh token (the lock,
    not a live PID in the row, is the authoritative ownership proof), writes
    the queued job artifact, and marks the sync lease COMPLETE. Any failure
    parks the row as REPAIR_PENDING so it stays unclaimable rather than
    running without its published record. A lock held by another publisher
    is ``busy``: the worker never waits for it.
    """

    repair_token = timestamped_token("record_sync", token_bytes=16)

    def claim(entries: list[QueueEntry]) -> tuple[tuple[str, QueueEntry | None], bool]:
        return _claim_repair_lease(entries, entry, repair_token=repair_token)

    def complete(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        return _complete_repair_lease(entries, entry, repair_token=repair_token)

    claimed = False
    try:
        # One lock acquisition covers claim, publication, and completion, so no
        # cancel fence or foreign publication can interleave between them.
        with ExitStack() as publication_lock:
            try:
                publication_lock.enter_context(
                    queue_record_publication_lock(queue_root, entry.queue_id, timeout_seconds=0.0)
                )
            except FileLockTimeoutError:
                # Another publisher still owns the lease; leave it untouched.
                return RepairOutcome(reason="busy", entry=entry)
            reason, current = mutate_entries(queue_root, claim)
            if reason != "claimed":
                _log_repair_refusal(entry, reason=reason, current=current)
                return RepairOutcome(reason=reason, entry=current)
            claimed = True
            assert current is not None
            upsert_row_job_record(cfg, current, STATUS_QUEUED, require_task_id=True)
            completed = mutate_entries(queue_root, complete)
    except BaseException as exc:
        _park_failed_repair_lease(queue_root, entry, repair_token=repair_token)
        if not isinstance(exc, Exception):
            raise
        logger.warning(
            "ORCA: queued record repair %s: queue_id=%s",
            "failed" if claimed else "claim failed",
            entry.queue_id,
            exc_info=True,
        )
        return RepairOutcome(
            reason="failed" if claimed else "claim_failed",
            entry=entry,
            error=exc,
        )
    logger.info("ORCA: repaired queued record publication: queue_id=%s", entry.queue_id)
    return RepairOutcome(reason="published", entry=completed)


def _record_publication_blocker(
    queue_root: Path, entry: QueueEntry, reason: str, *, require_unpublished: bool = False
) -> bool:
    """Record or clear a refusal without changing the publication generation."""
    blocker = (
        {
            "reason": reason,
            "scope": "orca_queue",
            "next_action": (
                "Inspect the worker log and restore the affected path/index access; the worker "
                f"retries automatically. To abandon this submission: queue cancel {entry.queue_id}."
            ),
        }
        if reason
        else None
    )

    def record(entries: list[QueueEntry]) -> tuple[None, bool]:
        for index, current in enumerate(entries):
            if current.queue_id != entry.queue_id:
                continue
            if (
                not same_generation(current, entry)
                or current.status != QueueStatus.PENDING
                or current.cancel_requested
                or (
                    require_unpublished
                    and queue_record_sync_state(current) == QUEUE_RECORD_SYNC_COMPLETE
                )
            ):
                return None, False
            if current.metadata.get(QUEUE_RECORD_SYNC_BLOCKED_KEY) == blocker:
                return None, False
            metadata = dict(current.metadata)
            metadata[QUEUE_RECORD_SYNC_BLOCKED_KEY] = blocker
            entries[index] = replace(current, metadata=metadata)
            return None, True
        return None, False

    try:
        mutate_entries(queue_root, record)
    except Exception:
        logger.exception("Failed to persist ORCA publication blocker: queue_id=%s", entry.queue_id)
        return False
    return not reason


def _orca_publication_job_dir_issue(queue_root: Path, entry: QueueEntry) -> str:
    """Return why a new snapshot no longer names its submission directory."""

    reaction_dir_text = queue_entry_reaction_dir(entry)
    if not reaction_dir_text:
        return "reaction_dir_missing"
    try:
        lexical_reaction_dir = Path(reaction_dir_text).expanduser().absolute()
        resolved_queue_root = queue_root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return "queue_or_reaction_path_invalid"
    if should_exclude_from_production_runs_scan(lexical_reaction_dir, resolved_queue_root):
        return "reaction_dir_reserved_or_unsafe"

    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    snapshot = metadata.get("execution_snapshot")
    if not isinstance(snapshot, Mapping):
        return "execution_snapshot_missing"
    identity = snapshot.get("job_dir_identity")
    if identity is None:
        return "job_dir_identity_missing"
    if not isinstance(identity, Mapping):
        return "job_dir_identity_invalid"
    try:
        expected_identity = (
            int(identity.get("device", -1)),
            int(identity.get("inode", -1)),
        )
        resolved_reaction_dir = lexical_reaction_dir.resolve(strict=True)
        named_status = lexical_reaction_dir.lstat()
    except (OSError, RuntimeError, TypeError, ValueError):
        return "job_dir_identity_unverifiable"
    if (
        lexical_reaction_dir != resolved_reaction_dir
        or not stat.S_ISDIR(named_status.st_mode)
        or (int(named_status.st_dev), int(named_status.st_ino)) != expected_identity
    ):
        return "job_dir_namespace_or_identity_changed"
    if not resolved_reaction_dir.is_relative_to(resolved_queue_root):
        return "reaction_dir_outside_queue_root"
    if should_exclude_from_production_runs_scan(resolved_reaction_dir, resolved_queue_root):
        return "reaction_dir_reserved_or_unsafe"
    return ""


def _fence_invalid_orca_publication(
    queue_root: Path,
    entry: QueueEntry,
    *,
    issue: str,
) -> bool:
    """Terminally fence an exact pending row whose bound job path changed."""

    try:
        fenced = mark_failed(
            queue_root,
            entry.queue_id,
            error=f"queue_publication_job_dir_invalid:{issue}",
            publish_terminal_side_effects=False,
            metadata_update=queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_ABORTED,
                token=queue_record_sync_token(entry),
                owner_pid=0,
            ),
            expected_entry=entry,
            expected_task_id=entry.task_id,
        )
    except Exception:  # Retain the row unclaimable on persistence failure.
        logger.exception(
            "Failed to fence ORCA publication with changed job path: queue_id=%s issue=%s",
            entry.queue_id,
            issue,
        )
        return False
    if fenced:
        logger.error(
            "Fenced ORCA publication whose bound job path changed: queue_id=%s issue=%s",
            entry.queue_id,
            issue,
        )
        return True
    current = get_entry_by_id(queue_root, entry.queue_id)
    return bool(
        current is None or current.status != QueueStatus.PENDING or current.cancel_requested
    )


def repair_queue_publication(
    cfg: AppConfig,
    queue_root: Path,
    entry: QueueEntry,
) -> bool:
    """Repair one queued-index publication before the row can be claimed."""
    if not is_orca_queue_entry(entry):
        return True
    if entry.status != QueueStatus.PENDING or entry.cancel_requested:
        return True
    job_dir_issue = _orca_publication_job_dir_issue(queue_root, entry)
    if job_dir_issue:
        if _fence_invalid_orca_publication(
            queue_root,
            entry,
            issue=job_dir_issue,
        ):
            return True
        return _record_publication_blocker(
            queue_root, entry, f"job directory fence failed: {job_dir_issue}"
        )
    reaction_dir_text = queue_entry_reaction_dir(entry)
    try:
        reaction_dir = Path(reaction_dir_text).expanduser().resolve()
        resolved_queue_root = queue_root.expanduser().resolve()
    except (OSError, RuntimeError):
        return _record_publication_blocker(
            queue_root, entry, "reaction or queue path cannot be resolved"
        )
    if not reaction_dir_text or not reaction_dir.is_relative_to(resolved_queue_root):
        return _record_publication_blocker(
            queue_root, entry, "reaction directory is outside the queue root"
        )
    for key in ("selected_inp", "selected_input_path", "selected_input_xyz"):
        selected_input_text = str(entry.metadata.get(key) or "").strip()
        if not selected_input_text:
            continue
        try:
            selected_input = Path(selected_input_text).expanduser().resolve()
        except (OSError, RuntimeError):
            return _record_publication_blocker(
                queue_root, entry, f"selected input path cannot be resolved: {key}"
            )
        if not selected_input.is_relative_to(reaction_dir):
            return _record_publication_blocker(
                queue_root, entry, f"selected input is outside the reaction directory: {key}"
            )
    state = queue_record_sync_state(entry)
    if not state or state == QUEUE_RECORD_SYNC_COMPLETE:
        if entry.metadata.get(QUEUE_RECORD_SYNC_BLOCKED_KEY):
            return _record_publication_blocker(queue_root, entry, "")
        return True
    if state not in REPAIRABLE_SYNC_STATES:
        logger.error(
            "Cannot repair ORCA queue publication with invalid state %r: %s",
            state,
            queue_entry_id(entry),
        )
        return _record_publication_blocker(queue_root, entry, f"invalid publication state: {state}")
    # The repair holds one publication-lock acquisition across claim,
    # publication, and completion, and it claims with a freshly minted token:
    # the lock, not process liveness or the recorded token, is the
    # authoritative ownership proof, and the original publisher is hard-fenced.
    outcome = repair_enqueue_publication_outcome(cfg, queue_root, entry)
    if outcome.reason == "busy":
        return False
    if not outcome.repaired:
        reason = outcome.reason
        if outcome.error is not None:
            reason = f"{reason}: {type(outcome.error).__name__}: {outcome.error}"
        return _record_publication_blocker(queue_root, entry, reason, require_unpublished=True)
    return True


def repair_queue_publications(cfg: AppConfig) -> frozenset[str] | None:
    """Return withheld queue IDs, or ``None`` when the queue cannot be inspected.

    A publication failure belongs to its row. Include refusals even for a
    COMPLETE lease: path validation or persisting its safety fence can fail,
    and the durable lease alone must not make that row eligible this pass.
    """
    root = roots.queue_root(cfg)
    try:
        entries = list_queue(root)
    except Exception:
        logger.exception("Failed to inspect ORCA publication repairs: %s", root)
        return None
    return frozenset(
        queue_entry_id(entry) for entry in entries if not repair_queue_publication(cfg, root, entry)
    )


__all__ = [
    "RepairOutcome",
    "repair_enqueue_publication_outcome",
    "repair_queue_publication",
    "repair_queue_publications",
]
