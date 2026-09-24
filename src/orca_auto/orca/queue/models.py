from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.core.queue.processes import ManagedProcess

from .terminal_replay import StateGenerationFingerprint


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

    @property
    def key(self) -> tuple[str, str]:
        return (str(self.queue_root), self.queue_id)


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
    than a fresh active-to-terminal transition.
    """

    pending_replays: dict[tuple[str, str], TerminalReplayWorkItem] = field(default_factory=dict)
    reconcile_statuses: dict[tuple[str, str], str] | None = None
    blocked_marker_keys: set[tuple[str, str]] = field(default_factory=set)
    generation_owners: dict[str, tuple[str, str]] = field(default_factory=dict)
    generation_owner_active: dict[str, bool] = field(default_factory=dict)
    # Kept current by the reserve gate, which runs before every reservation;
    # read by the row filter inside that reservation.
    admission_withheld_keys: frozenset[str] = frozenset()


__all__ = ["OrcaRunningJob", "OrcaWorkerReplayState", "TerminalReplayWorkItem"]
