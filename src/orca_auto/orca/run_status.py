"""Observed ORCA run status: what a queue row means right now.

A queue row parked at ``running`` only means "in progress" while the run lock
is held; without it the run was cancelled, killed or crashed. This is a domain
truth about ORCA runs, so it lives here rather than in any one presentation
layer.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orca_auto.core.queue.types import QueueStatus, effective_queue_status
from orca_auto.core.statuses import STATUS_PENDING
from orca_auto.core.utils import normalize_text
from orca_auto.core.utils.process_tracking import run_lock_is_held

from .queue.entries import queue_entry_reaction_dir
from .run_snapshot import RunSnapshot
from .statuses import ACTIVE_RUN_STATUS_VALUES

_LOGGER = logging.getLogger(__name__)


def observed_queue_status(entry: Any, snapshot: RunSnapshot | None) -> str:
    """The status a queue row shows: a ``running`` row without a live run lock is pending."""
    status = effective_queue_status(entry)
    if status != QueueStatus.RUNNING.value:
        return status
    snapshot_status = normalize_text(snapshot.status) if snapshot is not None else ""
    # An active run status implies a live process; only the run lock proves one.
    if snapshot_status and snapshot_status not in ACTIVE_RUN_STATUS_VALUES:
        return snapshot_status
    reaction_dir = normalize_text(queue_entry_reaction_dir(entry))
    if reaction_dir and not run_lock_is_held(Path(reaction_dir), logger=_LOGGER):
        return STATUS_PENDING
    return snapshot_status or status


__all__ = ["observed_queue_status"]
