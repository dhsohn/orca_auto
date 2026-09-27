"""Terminal settlement of one ORCA queue generation, step by step.

A generation that became terminal is settled in this order: mark the row
terminal with its replay marker (``mark_terminal_row``), prepare the terminal
``job_state.json`` and reports (``prepare``), bind the row's outcome and run id
to that state (``bind_row``), release the execution slot (the worker's step),
then ``finish``: the location record, the one notification claim and the
marker clear. The worker's live completion and cancellation call these steps
around its slot release; the restart pipeline in ``replay.py`` calls
``settle``. Every function takes one generation and its state explicitly.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from orca_auto.core.queue.child.process import entry_status_is_running
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.statuses import STATUS_CANCELLED, STATUS_COMPLETED, STATUS_FAILED

from ..config import AppConfig
from ..execution_binding import orca_execution_provenance
from ..state_reading import get_run_id_from_state
from . import job_records, notifications
from .adapter import (
    get_cancel_requested,
    get_entry_by_id,
    mark_cancelled,
    mark_completed,
    mark_failed,
    update_metadata,
    update_terminal,
)
from .entries import (
    TERMINAL_REPLAY_METADATA_KEY,
    queue_entry_id,
    queue_entry_metadata,
    queue_entry_reaction_dir,
    queue_entry_status,
    queue_entry_task_id,
)
from .models import TerminalReplayWorkItem
from .terminal_marker import (
    TerminalGenerationVerdict,
    load_state_generation_fingerprint,
    state_fingerprint_from_payload,
    terminal_generation_verdict,
    terminal_replay_marker_from_entry,
)
from .terminal_state import record_cancelled_run_state, record_failed_run_state

logger = logging.getLogger(__name__)


def reaction_key_for_dir(reaction_dir: str) -> str | None:
    if not reaction_dir:
        return None
    return str(Path(reaction_dir).expanduser().resolve())


def reaction_dir_key(entry: QueueEntry) -> str | None:
    return reaction_key_for_dir(queue_entry_reaction_dir(entry))


def mark_terminal_row(
    queue_root: Path,
    queue_id: str,
    *,
    task_id: str | None,
    reaction_dir: str,
    rc: int,
) -> bool:
    """Mark the running row of an exited child terminal, with its replay marker.

    The outcome is cancelled when cancellation was requested, else completed
    for exit code 0 and failed otherwise. Returns whether this call changed
    the row; a row that is no longer this generation's running row is left.
    """
    current = get_entry_by_id(queue_root, queue_id)
    current_task_id = queue_entry_task_id(current) if current is not None else None
    expected_job_id = current_task_id or task_id
    if current is None or not entry_status_is_running(current):
        logger.info("Skipping terminal mark for %s; entry is no longer running", queue_id)
        return False
    if task_id and current_task_id and task_id != current_task_id:
        logger.error("Skipping terminal mark for %s; running queue generation changed", queue_id)
        return False
    run_id = get_run_id_from_state(reaction_dir, expected_job_id=expected_job_id)
    if get_cancel_requested(
        queue_root, queue_id, expected_entry=current, expected_task_id=expected_job_id
    ):
        logger.info("Job cancelled: %s (rc=%d)", queue_id, rc)
        marked = mark_cancelled(
            queue_root, queue_id, expected_entry=current, expected_task_id=expected_job_id
        )
    elif rc == 0:
        logger.info("Job completed: %s (rc=%d)", queue_id, rc)
        marked = mark_completed(
            queue_root,
            queue_id,
            run_id=run_id,
            expected_entry=current,
            expected_task_id=expected_job_id,
        )
    else:
        logger.warning("Job failed: %s (rc=%d)", queue_id, rc)
        marked = mark_failed(
            queue_root,
            queue_id,
            error=f"exit_code={rc}",
            run_id=run_id,
            expected_entry=current,
            expected_task_id=expected_job_id,
        )
    if not marked:
        logger.info(
            "Skipping terminal finalization for %s; terminal mark did not update the entry",
            queue_id,
        )
    return bool(marked)


def new_work_item(
    queue_root: Path,
    entry: QueueEntry,
    *,
    reaction_dir: str,
    reaction_key: str,
) -> TerminalReplayWorkItem:
    """The immutable settlement snapshot of a terminal row, marker fields first."""
    metadata = queue_entry_metadata(entry)
    snapshot = metadata.get("execution_snapshot")
    try:
        execution_provenance = (
            orca_execution_provenance(snapshot) if isinstance(snapshot, Mapping) else None
        )
    except (TypeError, ValueError):
        execution_provenance = None
    marker = terminal_replay_marker_from_entry(entry)
    selected_inp = str(
        (marker or {}).get("selected_inp")
        or metadata.get("selected_inp")
        or metadata.get("selected_input_path")
        or ""
    ).strip()
    observed_state = state_fingerprint_from_payload((marker or {}).get("observed_state"))
    if observed_state is None:
        observed_state = load_state_generation_fingerprint(
            Path(reaction_dir).expanduser().resolve()
        )
    return TerminalReplayWorkItem(
        queue_root=Path(queue_root).expanduser().resolve(),
        queue_id=queue_entry_id(entry),
        reaction_dir=reaction_dir,
        reaction_key=reaction_key,
        task_id=str((marker or {}).get("task_id") or queue_entry_task_id(entry) or "").strip(),
        observed_status=queue_entry_status(entry),
        selected_inp=selected_inp,
        error=str((marker or {}).get("error") or entry.error or "queue_failed"),
        execution_provenance=execution_provenance,
        recorded_run_id=str(metadata.get("run_id") or "").strip(),
        observed_state=observed_state,
    )


def work_item_for_row(queue_root: Path, entry: QueueEntry | None) -> TerminalReplayWorkItem | None:
    """The work item a marked terminal row still owes; ``None`` when no marker is left."""
    if entry is None or terminal_replay_marker_from_entry(entry) is None:
        return None
    reaction_dir = queue_entry_reaction_dir(entry)
    reaction_key = reaction_dir_key(entry)
    if not reaction_dir or not reaction_key:
        raise RuntimeError(
            f"terminal replay marker has no durable reaction identity: {queue_entry_id(entry)}"
        )
    return new_work_item(queue_root, entry, reaction_dir=reaction_dir, reaction_key=reaction_key)


# The verdicts that prove a newer generation owns the directory. An unreadable
# state, an unverifiable mark and an unidentified state are left to the writer
# under run.lock, which fails closed.
_SUPERSEDING_VERDICTS = frozenset(
    {
        TerminalGenerationVerdict.DISAPPEARED,
        TerminalGenerationVerdict.RESTARTED,
        TerminalGenerationVerdict.NEWER_RUN,
        TerminalGenerationVerdict.REPLACED,
        TerminalGenerationVerdict.OTHER_ACTIVE,
        TerminalGenerationVerdict.UNOBSERVED_PREVIOUS_TERMINAL,
        TerminalGenerationVerdict.UNOBSERVED_OTHER_ACTIVE,
    }
)


def is_superseded(item: TerminalReplayWorkItem) -> bool:
    """Whether the reaction directory's state now belongs to a newer generation."""
    if not str(item.reaction_dir or "").strip():
        return True
    if not item.task_id:
        return True
    expected_run_id = str(item.run_id or item.recorded_run_id or "").strip()
    if (
        not expected_run_id
        and item.observed_state is not None
        and item.observed_state.job_id == item.task_id
    ):
        expected_run_id = item.observed_state.run_id
    verdict = terminal_generation_verdict(
        load_state_generation_fingerprint(Path(item.reaction_dir).expanduser().resolve()),
        task_id=item.task_id,
        observed=item.observed_state,
        expected_run_id=expected_run_id,
    )
    return verdict in _SUPERSEDING_VERDICTS


def prepare(item: TerminalReplayWorkItem) -> TerminalReplayWorkItem:
    """Resolve the terminal outcome and run identity, writing a missing terminal state.

    A completed row needs the child's own terminal state; a failed or
    cancelled row gets one synthesized under ``run.lock`` when the child left
    none. An already prepared item is returned as it is.
    """
    if item.state_prepared:
        return item
    run_id: str | None = None
    terminal_status: str | None = item.observed_status
    reaction_text = str(item.reaction_dir or "").strip()
    if not reaction_text:
        raise RuntimeError("terminal replay has no reaction directory")
    reaction_dir = Path(reaction_text).expanduser().resolve()
    if item.observed_status == STATUS_COMPLETED:
        # Exit code zero alone is not an execution result. Resolve the actual
        # outcome and run identity before the parent returns execution capacity.
        current = load_state_generation_fingerprint(reaction_dir)
        if not current.readable or current.job_id != item.task_id or not current.terminal_status:
            raise RuntimeError("terminal run state is not ready for the completed queue entry")
        run_id = current.run_id or None
        terminal_status = current.terminal_status
    elif item.observed_status == STATUS_FAILED:
        run_id, terminal_status = record_failed_run_state(
            reaction_dir,
            fallback_job_id=item.task_id,
            selected_inp=item.selected_inp,
            reason=item.error,
            observed_state=item.observed_state,
            execution_provenance=item.execution_provenance,
        )
    elif item.observed_status == STATUS_CANCELLED:
        run_id, terminal_status = record_cancelled_run_state(
            reaction_dir,
            fallback_job_id=item.task_id,
            selected_inp=item.selected_inp,
            observed_state=item.observed_state,
            execution_provenance=item.execution_provenance,
        )
    resolved_status = (
        terminal_status
        if terminal_status in (STATUS_COMPLETED, STATUS_CANCELLED)
        else STATUS_FAILED
    )
    return replace(item, resolved_status=resolved_status, run_id=run_id, state_prepared=True)


def bind_row(item: TerminalReplayWorkItem) -> TerminalReplayWorkItem:
    """Bind the queue row to the prepared run's actual outcome and identity."""
    current = get_entry_by_id(item.queue_root, item.queue_id)
    current_status = queue_entry_status(current) if current is not None else ""
    if item.resolved_status != current_status or (
        item.run_id and item.recorded_run_id != item.run_id
    ):
        updated = update_terminal(
            item.queue_root,
            item.queue_id,
            item.resolved_status,
            run_id=item.run_id,
            expected_task_id=item.task_id,
        )
        if not updated:
            logger.info(
                "Queue entry disappeared after terminal state preparation; "
                "continuing side effects: queue_id=%s",
                item.queue_id,
            )
        item = replace(item, recorded_run_id=item.run_id or item.recorded_run_id)
    return item


def clear_marker(item: TerminalReplayWorkItem) -> bool:
    return update_metadata(item.queue_root, item.queue_id, {TERMINAL_REPLAY_METADATA_KEY: None})


def retire_marker(item: TerminalReplayWorkItem) -> None:
    """Clear the row's replay marker, or confirm that no row still carries it."""
    if clear_marker(item):
        return
    current = get_entry_by_id(item.queue_root, item.queue_id)
    if current is None or terminal_replay_marker_from_entry(current) is None:
        return
    raise RuntimeError(
        f"terminal replay marker remained after successful side effects: queue_id={item.queue_id}"
    )


def _publish(cfg: AppConfig, item: TerminalReplayWorkItem) -> None:
    if not str(item.reaction_dir or "").strip():
        raise RuntimeError("terminal replay has no reaction directory")
    record_upserted = job_records.upsert_terminal_job_record(
        cfg,
        item.reaction_dir,
        fallback_job_id=item.task_id,
        expected_job_id=item.task_id,
    )
    if not record_upserted:
        raise RuntimeError("terminal job record artifacts are not ready")
    # The notification is advisory, as it is at submission: a delivery failure
    # is logged (redacted) by the notifier and does not retain the replay.
    # Retaining it would unnecessarily fence the next generation in this
    # directory even though execution capacity has already been returned.
    # The notifier durably claims one attempt before background delivery; it
    # never writes state after sending, when a successor may already own it.
    # A failed dispatch or delivery is one missed message, not a retry loop.
    # An exception out of the notifier is the same missed message:
    # only the record upsert above and the marker may retain the publication.
    try:
        notifications.claim_and_send_terminal(
            cfg,
            item.reaction_dir,
            expected_job_id=item.task_id,
            expected_run_id=item.run_id or item.recorded_run_id or None,
        )
    except Exception as exc:  # noqa: BLE001 - advisory boundary
        logger.warning(
            "Terminal notification raised and is not retried: reaction_dir=%s error=%s",
            item.reaction_dir,
            type(exc).__name__,
        )


def finish(cfg: AppConfig, item: TerminalReplayWorkItem) -> None:
    """Publish the location record and the advisory notification, then retire the marker.

    Failure leaves the durable generation pending; it needs no execution slot.
    """
    _publish(cfg, item)
    retire_marker(item)


def settle(
    cfg: AppConfig,
    item: TerminalReplayWorkItem,
    *,
    has_row: bool,
    pending_replays: dict[str, TerminalReplayWorkItem],
) -> TerminalReplayWorkItem:
    """Settle a generation found by the restart pipeline: prepare, bind, finish.

    Each step's item replaces the retained one in ``pending_replays``, so a
    failure retries from the last step that succeeded. Without a queue row
    (``has_row=False``) there is nothing to bind and no marker to retire.
    """
    item = prepare(item)
    pending_replays[item.queue_id] = item
    if not has_row:
        _publish(cfg, item)
        return item
    item = bind_row(item)
    pending_replays[item.queue_id] = item
    finish(cfg, item)
    return item


__all__ = [
    "bind_row",
    "clear_marker",
    "finish",
    "is_superseded",
    "mark_terminal_row",
    "new_work_item",
    "prepare",
    "reaction_dir_key",
    "reaction_key_for_dir",
    "retire_marker",
    "settle",
    "work_item_for_row",
]
