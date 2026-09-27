from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.core.queue.processes import ManagedProcess

from .terminal_marker import StateGenerationFingerprint


@dataclass(frozen=True)
class TerminalReplayWorkItem:
    """One durable terminal generation whose side effects must still be replayed."""

    queue_root: Path
    queue_id: str
    reaction_dir: str
    reaction_key: str
    task_id: str
    observed_status: str
    selected_inp: str
    error: str
    execution_provenance: dict[str, Any] | None = None
    recorded_run_id: str = ""
    resolved_status: str = ""
    run_id: str | None = None
    state_prepared: bool = False
    observed_state: StateGenerationFingerprint | None = None


@dataclass
class OrcaRunningJob:
    queue_root: Path
    queue_id: str
    reaction_dir: str
    process: ManagedProcess
    admission_token: str
    task_id: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    pending_terminal_replay: TerminalReplayWorkItem | None = None
    terminal_finalize_pending: bool = False


@dataclass
class OrcaWorkerReplayState:
    """The worker's terminal-replay bookkeeping, in one typed place.

    ``OrcaQueueWorker`` creates one instance during construction. ``reconcile_statuses`` stays
    ``None`` until the first reconcile pass seeds the startup cursor, so a
    terminal row first seen after startup is treated as closed history rather
    than a fresh active-to-terminal transition. ``retry_keys`` holds the rows
    whose observed transition the last pass could not replay (an ambiguous
    generation owner or failed side effects); the next pass treats them as
    still observed. A key leaves the set when its row settles or drops.
    """

    # Every map is keyed by queue_id; ``generation_owners`` maps a reaction key to one.
    pending_replays: dict[str, TerminalReplayWorkItem] = field(default_factory=dict)
    reconcile_statuses: dict[str, str] | None = None
    retry_keys: set[str] = field(default_factory=set)
    blocked_marker_keys: set[str] = field(default_factory=set)
    generation_owners: dict[str, str] = field(default_factory=dict)
    generation_owner_active: dict[str, bool] = field(default_factory=dict)


__all__ = ["OrcaRunningJob", "OrcaWorkerReplayState", "TerminalReplayWorkItem"]
