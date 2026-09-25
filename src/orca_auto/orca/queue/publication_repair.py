from __future__ import annotations

import logging
import stat
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from orca_auto.core.paths import should_exclude_from_production_runs_scan
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_BLOCKED_KEY,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QUEUE_RECORD_SYNC_REPAIRING,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    queue_record_sync_metadata,
    queue_record_sync_state,
)
from orca_auto.core.queue.store import mutate_entries
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.queue.enqueue_publication import repair_enqueue_publication_outcome
from orca_auto.orca.queue.identity import entry_matches_engine_identity

from ..config import AppConfig
from .adapter import (
    get_entry_by_id,
    list_queue,
    mark_failed,
    queue_entries_same_publication_generation,
)
from .entries import queue_entry_id, queue_entry_is_retired_workflow_owned, queue_entry_reaction_dir
from .roots import queue_roots
from .worker_tracking import upsert_queued_job_record

logger = logging.getLogger(__name__)


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
                not queue_entries_same_publication_generation(current, entry)
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

    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    token = str(metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY) or "").strip()
    try:
        fenced = mark_failed(
            queue_root,
            entry.queue_id,
            error=f"queue_publication_job_dir_invalid:{issue}",
            publish_terminal_side_effects=False,
            metadata_update=queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_ABORTED,
                token=token,
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
    if not entry_matches_engine_identity(entry, "orca"):
        return True
    if entry.status != QueueStatus.PENDING or entry.cancel_requested:
        return True
    if queue_entry_is_retired_workflow_owned(entry, queue_root):
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
    if state not in {
        QUEUE_RECORD_SYNC_PREPARING,
        QUEUE_RECORD_SYNC_REPAIR_PENDING,
        QUEUE_RECORD_SYNC_REPAIRING,
    }:
        logger.error(
            "Cannot repair ORCA queue publication with invalid state %r: %s",
            state,
            queue_entry_id(entry),
        )
        return _record_publication_blocker(queue_root, entry, f"invalid publication state: {state}")
    # The shared repair driver holds one publication-lock acquisition across
    # claim, publication, and completion, and it claims with a freshly minted
    # token: the lock, not process liveness or the recorded token, is the
    # authoritative ownership proof, and the original publisher is hard-fenced.
    outcome = repair_enqueue_publication_outcome(
        queue_root,
        entry,
        publish=lambda current: upsert_queued_job_record(cfg, current),
        label="ORCA",
        same_generation=queue_entries_same_publication_generation,
        lock_timeout_seconds=0.0,
    )
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
    withheld_ids: set[str] = set()
    for queue_root in queue_roots(cfg):
        try:
            entries = list_queue(queue_root)
        except Exception:
            logger.exception("Failed to inspect ORCA publication repairs: %s", queue_root)
            return None
        for entry in entries:
            if not repair_queue_publication(cfg, queue_root, entry):
                withheld_ids.add(queue_entry_id(entry))
    return frozenset(withheld_ids)


__all__ = ["repair_queue_publication", "repair_queue_publications"]
