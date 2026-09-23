from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from orca_auto.core.queue.processes import ManagedProcess

if TYPE_CHECKING:
    from .replay import TerminalReplayWorkItem


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
