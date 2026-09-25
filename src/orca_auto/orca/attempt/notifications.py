from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..notifications import dispatch_notification
from ..statuses import RunStatus
from ..types import RunStartedNotification, RunState
from .reporting import build_run_started_notification


@dataclass(frozen=True)
class AttemptStartedNotification:
    reaction_dir: Path
    selected_inp: Path
    current_inp: Path
    state: RunState
    execution_index: int
    first_execution_index: int
    status: RunStatus
    attempt_started_at: str
    resumed: bool
    notify_started: Any | None


def notify_attempt_started(ctx: AttemptStartedNotification) -> None:
    should_notify_started = ctx.execution_index == ctx.first_execution_index and (
        ctx.execution_index == 1 or ctx.resumed
    )
    if not should_notify_started or ctx.notify_started is None:
        return

    notification: RunStartedNotification = build_run_started_notification(
        reaction_dir=ctx.reaction_dir,
        selected_inp=ctx.selected_inp,
        current_inp=ctx.current_inp,
        state=ctx.state,
        execution_index=ctx.execution_index,
        status=ctx.status,
        attempt_started_at=ctx.attempt_started_at,
        resumed=ctx.resumed,
    )
    notify = ctx.notify_started
    dispatch_notification(lambda: notify(notification), kind="started")


__all__ = [
    "AttemptStartedNotification",
    "notify_attempt_started",
]
