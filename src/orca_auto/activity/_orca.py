"""The queue catalog: each ORCA queue row joined with its own root state."""

from __future__ import annotations

from pathlib import Path

from orca_auto.activity.model import ActivityRecord, path_aliases, timestamp_metadata, unique_texts
from orca_auto.core.queue.deferral import queue_entry_admission_deferral_reason
from orca_auto.core.queue.publication import QUEUE_RECORD_SYNC_BLOCKED_KEY
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.statuses import ACTIVE_STATUSES, STATUS_PENDING
from orca_auto.core.utils import normalize_text
from orca_auto.orca.app_ids import ORCA_AUTO_ORCA_SOURCE, ORCA_ENGINE
from orca_auto.orca.queue import adapter as queue_adapter
from orca_auto.orca.queue import entries as queue_entries
from orca_auto.orca.queue.terminal_marker import (
    TERMINAL_PUBLICATION_INVALID_ACTION,
    TERMINAL_PUBLICATION_INVALID_REASON,
    TERMINAL_PUBLICATION_OWNER,
    TERMINAL_PUBLICATION_PENDING_ACTION,
    TERMINAL_PUBLICATION_PENDING_REASON,
    TERMINAL_PUBLICATION_SCOPE,
    TerminalReplayMarkerKind,
    terminal_replay_marker_kind,
)
from orca_auto.orca.run_snapshot import RunSnapshot, collect_run_snapshots
from orca_auto.orca.run_status import observed_queue_status


def _resolved_dir(path_text: str) -> str:
    try:
        return str(Path(path_text).expanduser().resolve())
    except OSError:
        return path_text


def _row_snapshot(entry: QueueEntry, snapshot_by_dir: dict[str, RunSnapshot]) -> RunSnapshot | None:
    """The row's own root state, when it belongs to this row's run or generation."""
    reaction_dir = queue_entries.queue_entry_reaction_dir(entry)
    snapshot = snapshot_by_dir.get(_resolved_dir(reaction_dir)) if reaction_dir else None
    if snapshot is None:
        return None
    run_id = normalize_text(queue_entries.queue_entry_run_id(entry))
    if run_id:
        return snapshot if normalize_text(snapshot.run_id) == run_id else None
    if normalize_text(queue_entries.queue_entry_status(entry)) not in ACTIVE_STATUSES:
        return None
    # The reusable root can still carry the preceding run's terminal state
    # while this queue generation waits for its child to publish new state.
    if snapshot.state_generation_identity != queue_entries.queue_entry_generation_token(entry):
        return None
    return snapshot


def queue_record(
    entry: QueueEntry,
    snapshot: RunSnapshot | None,
    *,
    allowed_root: Path,
) -> ActivityRecord:
    entry_metadata = queue_entries.queue_entry_metadata(entry)
    queue_id = normalize_text(queue_entries.queue_entry_id(entry))
    task_id = normalize_text(queue_entries.queue_entry_task_id(entry))
    run_id = normalize_text(queue_entries.queue_entry_run_id(entry))
    reaction_dir = normalize_text(queue_entries.queue_entry_reaction_dir(entry))
    snapshot_name = snapshot.name if snapshot is not None else ""
    snapshot_completed_at = snapshot.completed_at if snapshot is not None else ""
    snapshot_updated_at = snapshot.updated_at if snapshot is not None else ""
    # A running row carries its run ID only in the state of its own generation.
    snapshot_run_id = normalize_text(snapshot.run_id) if snapshot is not None else ""
    status = observed_queue_status(entry, snapshot)
    blocker = entry_metadata.get(QUEUE_RECORD_SYNC_BLOCKED_KEY)
    if not isinstance(blocker, dict) or status != STATUS_PENDING or entry.cancel_requested:
        blocker = {}
    replay_kind = terminal_replay_marker_kind(entry)
    if replay_kind != TerminalReplayMarkerKind.ABSENT:
        invalid = replay_kind == TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED
        blocker = {
            "reason": TERMINAL_PUBLICATION_INVALID_REASON
            if invalid
            else TERMINAL_PUBLICATION_PENDING_REASON,
            "scope": TERMINAL_PUBLICATION_SCOPE,
            "next_action": TERMINAL_PUBLICATION_INVALID_ACTION
            if invalid
            else TERMINAL_PUBLICATION_PENDING_ACTION,
        }
    label = (
        normalize_text(snapshot_name)
        or normalize_text(Path(reaction_dir).name if reaction_dir else "")
        or queue_id
        or task_id
    )
    submitted_at = normalize_text(getattr(entry, "enqueued_at", ""))
    started_at = normalize_text(getattr(entry, "started_at", ""))
    finished_at = normalize_text(getattr(entry, "finished_at", ""))
    updated_at = (
        normalize_text(snapshot_completed_at)
        or normalize_text(snapshot_updated_at)
        or finished_at
        or started_at
        or submitted_at
    )
    return ActivityRecord(
        activity_id=queue_id or run_id or task_id or label,
        kind="job",
        engine=ORCA_ENGINE,
        status=status,
        label=label,
        source=ORCA_AUTO_ORCA_SOURCE,
        submitted_at=submitted_at,
        updated_at=updated_at,
        cancel_target=queue_id or run_id or reaction_dir,
        worker_log=normalize_text(entry_metadata.get("worker_log")),
        aliases=unique_texts(
            [
                queue_id,
                task_id,
                run_id,
                snapshot_run_id,
                *list(path_aliases(reaction_dir, root=allowed_root)),
            ]
        ),
        ids=unique_texts([queue_id, run_id, snapshot_run_id]),
        metadata={
            "queue_id": queue_id,
            "task_id": task_id,
            "task_kind": normalize_text(getattr(entry, "task_kind", "")),
            "run_id": run_id,
            "job_type": normalize_text(entry_metadata.get("job_type")),
            "selected_inp": normalize_text(entry_metadata.get("selected_inp")),
            "reaction_dir": reaction_dir,
            "allowed_root": str(allowed_root),
            "priority": queue_entries.queue_entry_priority(entry),
            # A claim removes the deferral; a row that left pending by another
            # path must not advertise one either.
            "admission_deferral_reason": (
                queue_entry_admission_deferral_reason(entry) if status == STATUS_PENDING else ""
            ),
            "publication_blocked_reason": normalize_text(blocker.get("reason")),
            "publication_blocked_scope": normalize_text(blocker.get("scope")),
            "publication_blocked_action": normalize_text(blocker.get("next_action")),
            "publication_owner": TERMINAL_PUBLICATION_OWNER if blocker else "",
            **timestamp_metadata(
                enqueued_at=submitted_at, started_at=started_at, finished_at=finished_at
            ),
        },
    )


def catalog(allowed_root: Path) -> list[tuple[QueueEntry, ActivityRecord]]:
    """Every ORCA queue row with the record ``queue list`` shows for it.

    A row reads only its own directory's root ``job_state.json``; neither the
    location index nor any other directory under ``allowed_root`` is read.
    """
    entries = queue_adapter.list_queue(allowed_root)
    known_dirs = [
        Path(reaction_dir)
        for entry in entries
        if (reaction_dir := queue_entries.queue_entry_reaction_dir(entry))
    ]
    snapshot_by_dir = {
        _resolved_dir(str(snapshot.reaction_dir)): snapshot
        for snapshot in collect_run_snapshots(allowed_root, known_dirs=known_dirs)
    }
    return [
        (
            entry,
            queue_record(entry, _row_snapshot(entry, snapshot_by_dir), allowed_root=allowed_root),
        )
        for entry in entries
    ]
