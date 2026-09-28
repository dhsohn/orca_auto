"""The worker child's side of one job: its shutdown flag and the parent's slot hand-off."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orca_auto.core.admission import get_slot


@dataclass
class ChildWorkerShutdownController:
    requested: bool = False

    def request(self) -> None:
        self.requested = True

    def is_requested(self) -> bool:
        return self.requested


def await_parent_admission_handoff(
    admission_root: str | Path,
    admission_token: str,
    *,
    timeout_seconds: float = 10.0,
    monotonic_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait until the parent atomically attaches this child PID to the slot."""
    deadline = monotonic_fn() + max(0.0, float(timeout_seconds))
    while True:
        slot = get_slot(admission_root, admission_token)
        if slot is None:
            return False
        if slot.owner_pid == os.getpid():
            return True
        if monotonic_fn() >= deadline:
            return False
        sleep_fn(0.01)


__all__ = ["ChildWorkerShutdownController", "await_parent_admission_handoff"]
