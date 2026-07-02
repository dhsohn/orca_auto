from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orca_auto.core.app_ids import ORCA_AUTO_ORCA_SOURCE
from orca_auto.core.statuses import (
    STATUS_PENDING,
    STATUS_RETRYING,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
)
from orca_auto.core.utils import normalize_text
from orca_auto.core.utils.process_tracking import active_run_lock_pid

from ._activity_model import ActivityRecord

if TYPE_CHECKING:
    from orca_auto.orca.run_snapshot import RunSnapshot

_LOGGER = logging.getLogger(__name__)
_ORCA_ACTIVE_QUEUE_STATUSES = frozenset({STATUS_PENDING, STATUS_RUNNING})
_ORCA_TERMINAL_QUEUE_STATUSES = TERMINAL_STATUSES
# Snapshot run states that imply a live process; without a live run lock the run
# was cancelled/killed/crashed and must not keep showing as in progress.
_STALE_SNAPSHOT_STATUSES = frozenset({STATUS_RUNNING, STATUS_RETRYING})


def snapshot_matches_entry(
    queue_adapter: Any,
    entry: Any,
    snapshot_by_run_id: dict[str, RunSnapshot],
    snapshot_by_dir: dict[str, RunSnapshot],
) -> RunSnapshot | None:
    run_id = normalize_text(queue_adapter.queue_entry_run_id(entry))
    if run_id:
        return snapshot_by_run_id.get(run_id)
    if normalize_text(queue_adapter.queue_entry_status(entry)) not in _ORCA_ACTIVE_QUEUE_STATUSES:
        return None
    reaction_dir = normalize_text(queue_adapter.queue_entry_reaction_dir(entry))
    if not reaction_dir:
        return None
    try:
        resolved = str(Path(reaction_dir).expanduser().resolve())
    except OSError:
        resolved = reaction_dir
    return snapshot_by_dir.get(resolved)


def queue_represents_snapshot(queue_adapter: Any, entry: Any, snapshot: RunSnapshot | None) -> bool:
    if snapshot is None:
        return False
    run_id = normalize_text(queue_adapter.queue_entry_run_id(entry))
    if run_id and run_id == normalize_text(snapshot.run_id):
        return True
    if normalize_text(queue_adapter.queue_entry_status(entry)) not in _ORCA_ACTIVE_QUEUE_STATUSES:
        return False
    reaction_dir = normalize_text(queue_adapter.queue_entry_reaction_dir(entry))
    try:
        resolved = str(Path(reaction_dir).expanduser().resolve())
    except OSError:
        resolved = reaction_dir
    return resolved == normalize_text(snapshot.reaction_dir.resolve())


def snapshot_indexes(
    snapshots: list[RunSnapshot],
) -> tuple[dict[str, RunSnapshot], dict[str, RunSnapshot]]:
    snapshot_by_run_id = {
        normalize_text(snapshot.run_id): snapshot
        for snapshot in snapshots
        if normalize_text(snapshot.run_id)
    }
    snapshot_by_dir: dict[str, RunSnapshot] = {}
    for snapshot in snapshots:
        try:
            snapshot_by_dir[str(Path(snapshot.reaction_dir).expanduser().resolve())] = snapshot
        except OSError:
            continue
    return snapshot_by_run_id, snapshot_by_dir


def queue_entry_status(queue_adapter: Any, entry: Any, snapshot: RunSnapshot | None) -> str:
    status = normalize_text(queue_adapter.queue_entry_status(entry)) or "unknown"
    if bool(getattr(entry, "cancel_requested", False)) and status == "running":
        return "cancel_requested"
    if snapshot is None or status != "running":
        return status
    snapshot_status = normalize_text(snapshot.status)
    return snapshot_status if snapshot_status and snapshot_status != "running" else status


def queue_record(
    queue_adapter: Any,
    entry: Any,
    snapshot: RunSnapshot | None,
    *,
    allowed_root: Path,
    deps: Any,
) -> ActivityRecord:
    entry_metadata_loader = getattr(queue_adapter, "queue_entry_metadata", None)
    entry_metadata = dict(entry_metadata_loader(entry)) if callable(entry_metadata_loader) else {}
    queue_id = normalize_text(queue_adapter.queue_entry_id(entry))
    task_id = normalize_text(queue_adapter.queue_entry_task_id(entry))
    run_id = normalize_text(queue_adapter.queue_entry_run_id(entry))
    reaction_dir = normalize_text(queue_adapter.queue_entry_reaction_dir(entry))
    snapshot_name = snapshot.name if snapshot is not None else ""
    snapshot_completed_at = snapshot.completed_at if snapshot is not None else ""
    snapshot_updated_at = snapshot.updated_at if snapshot is not None else ""
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
        engine="orca",
        status=queue_entry_status(queue_adapter, entry, snapshot),
        label=label,
        source=ORCA_AUTO_ORCA_SOURCE,
        submitted_at=submitted_at,
        updated_at=updated_at,
        cancel_target=queue_id or run_id or reaction_dir,
        aliases=deps._unique_texts(
            [queue_id, task_id, run_id, *list(deps._path_aliases(reaction_dir, root=allowed_root))]
        ),
        metadata={
            "queue_id": queue_id,
            "task_id": task_id,
            "task_kind": normalize_text(getattr(entry, "task_kind", "")),
            "run_id": run_id,
            "job_type": normalize_text(entry_metadata.get("job_type")),
            "selected_inp": normalize_text(entry_metadata.get("selected_inp")),
            "workflow_id": normalize_text(entry_metadata.get("workflow_id")),
            "reaction_dir": reaction_dir,
            "allowed_root": str(allowed_root),
            "priority": queue_adapter.queue_entry_priority(entry),
            **deps._timestamp_metadata(
                enqueued_at=submitted_at, started_at=started_at, finished_at=finished_at
            ),
        },
    )


def snapshot_reaction_dir(snapshot: RunSnapshot) -> str:
    try:
        return str(snapshot.reaction_dir.expanduser().resolve())
    except OSError:
        return str(snapshot.reaction_dir)


def _snapshot_display_status(snapshot: RunSnapshot) -> str:
    status = normalize_text(snapshot.status).lower() or "unknown"
    if status not in _STALE_SNAPSHOT_STATUSES:
        return status
    reaction_dir = snapshot_reaction_dir(snapshot)
    if not reaction_dir:
        return status
    # An orphan snapshot still parked at a running status with no live run lock is
    # stale (the run was cancelled/killed/crashed); surface it as failed instead of
    # leaving it stuck "in progress" in the activity list.
    if active_run_lock_pid(Path(reaction_dir), logger=_LOGGER) is None:
        return "failed"
    return status


def snapshot_record(snapshot: RunSnapshot, *, allowed_root: Path, deps: Any) -> ActivityRecord:
    reaction_dir = snapshot_reaction_dir(snapshot)
    run_id = normalize_text(snapshot.run_id)
    label = (
        normalize_text(snapshot.name)
        or normalize_text(Path(reaction_dir).name if reaction_dir else "")
        or run_id
    )
    started_at = normalize_text(snapshot.started_at)
    completed_at = normalize_text(snapshot.completed_at)
    return ActivityRecord(
        activity_id=run_id or label,
        kind="job",
        engine="orca",
        status=_snapshot_display_status(snapshot),
        label=label,
        source=ORCA_AUTO_ORCA_SOURCE,
        submitted_at=started_at,
        updated_at=completed_at or normalize_text(snapshot.updated_at) or started_at,
        cancel_target=run_id or reaction_dir,
        aliases=deps._unique_texts(
            [
                run_id,
                *list(deps._path_aliases(reaction_dir, root=allowed_root)),
                normalize_text(snapshot.name),
            ]
        ),
        metadata={
            "run_id": run_id,
            "reaction_dir": reaction_dir,
            "allowed_root": str(allowed_root),
            "attempts": snapshot.attempts,
            "selected_inp_name": normalize_text(snapshot.selected_inp_name),
            # RunSnapshot carries no job_type; orphan-snapshot rows have always had
            # this empty. Kept for metadata-shape parity with queue_record.
            "job_type": "",
            **deps._timestamp_metadata(started_at=started_at, finished_at=completed_at),
        },
    )


def _resolved_entry_reaction_dir(queue_adapter: Any, entry: Any) -> str:
    reaction_dir = normalize_text(queue_adapter.queue_entry_reaction_dir(entry))
    if not reaction_dir:
        return ""
    try:
        return str(Path(reaction_dir).expanduser().resolve())
    except OSError:
        return reaction_dir


def superseded_snapshot_dirs(queue_adapter: Any, entries: list[Any]) -> set[str]:
    """Reaction dirs whose only queue state is terminal.

    A finished/cancelled queue entry supersedes any run snapshot still parked at
    "running" for the same dir. Without this a cancelled job keeps showing as in
    progress, because its stale snapshot is listed as a separate active row even
    though the queue already recorded a terminal outcome.
    """
    active: set[str] = set()
    terminal: set[str] = set()
    for entry in entries:
        reaction_dir = _resolved_entry_reaction_dir(queue_adapter, entry)
        if not reaction_dir:
            continue
        status = normalize_text(queue_adapter.queue_entry_status(entry))
        if status in _ORCA_ACTIVE_QUEUE_STATUSES:
            active.add(reaction_dir)
        elif status in _ORCA_TERMINAL_QUEUE_STATUSES:
            terminal.add(reaction_dir)
    return terminal - active


def _snapshot_is_superseded(snapshot: RunSnapshot, superseded_dirs: set[str]) -> bool:
    """Whether a stale snapshot is superseded by a terminal queue entry.

    A snapshot is only superseded when its dir has a terminal-only queue outcome
    *and* the run is no longer live. A live run lock means a genuinely running
    re-run shares the dir with an older terminal entry; suppressing it would hide
    an in-progress job, so defer to the live process and keep the row.
    """
    reaction_dir = snapshot_reaction_dir(snapshot)
    if reaction_dir not in superseded_dirs:
        return False
    return active_run_lock_pid(Path(reaction_dir), logger=_LOGGER) is None


def orca_records(
    *,
    config_path: str,
    deps: Any,
) -> list[ActivityRecord]:
    from orca_auto.orca import queue_adapter, run_snapshot

    runtime_paths = deps.engine_runtime_paths(config_path, engine="orca")
    allowed_root = runtime_paths["allowed_root"]
    reconcile = getattr(queue_adapter, "reconcile_orphaned_running_entries", None)
    if callable(reconcile):
        reconcile(allowed_root)

    queue_entries = list(queue_adapter.list_queue(allowed_root))
    snapshots = list(run_snapshot.collect_run_snapshots(allowed_root))
    snapshot_by_run_id, snapshot_by_dir = snapshot_indexes(snapshots)
    represented_snapshot_keys: set[str] = set()
    superseded_dirs = superseded_snapshot_dirs(queue_adapter, queue_entries)
    rows: list[ActivityRecord] = []

    for entry in queue_entries:
        snapshot = snapshot_matches_entry(queue_adapter, entry, snapshot_by_run_id, snapshot_by_dir)
        rows.append(
            queue_record(queue_adapter, entry, snapshot, allowed_root=allowed_root, deps=deps)
        )
        if snapshot is not None and queue_represents_snapshot(queue_adapter, entry, snapshot):
            represented_snapshot_keys.add(normalize_text(snapshot.key))

    for snapshot in snapshots:
        snapshot_key = normalize_text(snapshot.key)
        if snapshot_key and snapshot_key in represented_snapshot_keys:
            continue
        if _snapshot_is_superseded(snapshot, superseded_dirs):
            continue
        rows.append(snapshot_record(snapshot, allowed_root=allowed_root, deps=deps))

    return rows
