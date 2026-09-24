"""Observed ORCA run status: what a queue row or run snapshot means right now.

A queue row parked at ``running`` and a snapshot parked at an active run
status only mean "in progress" while the run lock is held; without it the run
was cancelled, killed or crashed. A terminal queue outcome for a directory
supersedes a stale snapshot of the same directory. These are domain truths
about ORCA runs, so they live here rather than in any one presentation layer.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orca_auto.core.queue.types import QueueStatus, effective_queue_status
from orca_auto.core.statuses import (
    ACTIVE_STATUSES,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_UNKNOWN,
    TERMINAL_STATUSES,
)
from orca_auto.core.utils import normalize_text
from orca_auto.core.utils.process_tracking import run_lock_is_held

from .run_snapshot import RunSnapshot
from .statuses import ACTIVE_RUN_STATUS_VALUES

_LOGGER = logging.getLogger(__name__)
_ORCA_ACTIVE_QUEUE_STATUSES = ACTIVE_STATUSES
_ORCA_TERMINAL_QUEUE_STATUSES = TERMINAL_STATUSES
# Snapshot run states that imply a live process; without a live run lock the run
# was cancelled/killed/crashed and must not keep showing as in progress.
STALE_SNAPSHOT_STATUSES = ACTIVE_RUN_STATUS_VALUES


def snapshot_reaction_dir(snapshot: RunSnapshot) -> str:
    try:
        return str(snapshot.reaction_dir.expanduser().resolve())
    except OSError:
        return str(snapshot.reaction_dir)


def queue_entry_status(queue_adapter: Any, entry: Any, snapshot: RunSnapshot | None) -> str:
    status = effective_queue_status(entry)
    if status != QueueStatus.RUNNING.value:
        return status
    snapshot_status = normalize_text(snapshot.status) if snapshot is not None else ""
    if snapshot_status and snapshot_status not in STALE_SNAPSHOT_STATUSES:
        return snapshot_status
    reaction_dir = normalize_text(queue_adapter.queue_entry_reaction_dir(entry))
    if reaction_dir and not run_lock_is_held(Path(reaction_dir), logger=_LOGGER):
        return STATUS_PENDING
    return snapshot_status or status


def snapshot_display_status(snapshot: RunSnapshot) -> str:
    status = normalize_text(snapshot.status).lower() or STATUS_UNKNOWN
    if status not in STALE_SNAPSHOT_STATUSES:
        return status
    reaction_dir = snapshot_reaction_dir(snapshot)
    if not reaction_dir:
        return status
    # An orphan snapshot still parked at a running status with no live run lock is
    # stale (the run was cancelled/killed/crashed); surface it as failed instead of
    # leaving it stuck "in progress" in the activity list.
    if not run_lock_is_held(Path(reaction_dir), logger=_LOGGER):
        return STATUS_FAILED
    return status


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


def snapshot_is_superseded(snapshot: RunSnapshot, superseded_dirs: set[str]) -> bool:
    """Whether a stale snapshot is superseded by a terminal queue entry.

    A snapshot is only superseded when its dir has a terminal-only queue outcome
    *and* the run is no longer live. A live run lock means a genuinely running
    re-run shares the dir with an older terminal entry; suppressing it would hide
    an in-progress job, so defer to the live process and keep the row.
    """
    reaction_dir = snapshot_reaction_dir(snapshot)
    if reaction_dir not in superseded_dirs:
        return False
    return not run_lock_is_held(Path(reaction_dir), logger=_LOGGER)


__all__ = [
    "STALE_SNAPSHOT_STATUSES",
    "queue_entry_status",
    "snapshot_display_status",
    "snapshot_is_superseded",
    "snapshot_reaction_dir",
    "superseded_snapshot_dirs",
]
