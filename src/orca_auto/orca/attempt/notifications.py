from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..statuses import RunStatus
from ..types import RunStartedNotification, RunState
from .reporting import build_run_started_notification

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttemptStartedNotification:
    reaction_dir: Path
    selected_inp: Path
    current_inp: Path
    state: RunState
    execution_index: int
    first_execution_index: int
    max_retries: int
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
        max_retries=ctx.max_retries,
        status=ctx.status,
        attempt_started_at=ctx.attempt_started_at,
        resumed=ctx.resumed,
    )
    try:
        ctx.notify_started(notification)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Started notification callback failed for attempt %d",
            ctx.execution_index,
            exc_info=True,
        )


__all__ = [
    "AttemptStartedNotification",
    "notify_attempt_started",
]
