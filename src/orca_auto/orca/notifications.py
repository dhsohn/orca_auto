"""ORCA run-lifecycle notifications.

Builders turn a lifecycle event into a messenger-neutral
:class:`~orca_auto.core.messaging.Message`; the ``notify_*`` helpers deliver it
through the :class:`~orca_auto.core.messaging.MessageChannel` that
:func:`notification_channel` resolves from the app config. The Discord renderer
owns the native markup, so these builders never see it. The identity is carried
on ``Message.author`` (the Discord embed author line), keeping it out of the
title.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orca_auto.core.messaging import (
    Message,
    MessageChannel,
    Severity,
    build_channel,
    code,
    field_row,
    group,
    raw,
    text,
)

from .statuses import RunStatus

if TYPE_CHECKING:
    from .types import (
        QueueEnqueuedNotification,
        RunFinalResult,
        RunFinishedNotification,
        RunStartedNotification,
        RunState,
    )

logger = logging.getLogger(__name__)


_NOTIFICATION_SLOTS = threading.BoundedSemaphore(4)


def dispatch_notification(deliver: Callable[[], object], *, kind: str) -> bool:
    """Attempt advisory delivery without blocking execution or interpreter shutdown.

    Callers settle ownership and capture the event before dispatch. A saturated
    sender or process exit can lose a message; the sender never mutates run state.
    """
    slots = _NOTIFICATION_SLOTS
    if not slots.acquire(blocking=False):
        logger.warning("%s notification skipped: delivery capacity exhausted", kind)
        return False

    def send() -> None:
        try:
            deliver()
        except Exception as exc:  # noqa: BLE001 - advisory transport
            logger.warning("%s notification failed: %s", kind, type(exc).__name__)
        finally:
            slots.release()

    try:
        threading.Thread(target=send, name=f"orca-{kind}-notification", daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - no retry after ownership was settled
        slots.release()
        logger.warning("%s notification dispatch failed: %s", kind, type(exc).__name__)
        return False
    return True


def notification_channel(cfg: Any) -> MessageChannel:
    """Resolve the outbound channel for ``cfg.messenger`` (a null channel when unset)."""
    return build_channel(cfg.messenger, logger=logger)


# --------------------------------------------------------------------------- #
# Run lifecycle message builders
# --------------------------------------------------------------------------- #
def build_run_finished_notification(
    *,
    reaction_dir: Path,
    selected_inp: Path,
    state: RunState,
    status: RunStatus | str,
    final_result: RunFinalResult,
) -> RunFinishedNotification:
    attempts = state.get("attempts")
    final_status = str(
        final_result.get("status", status.value if isinstance(status, RunStatus) else str(status))
    )
    analyzer_status = str(final_result.get("analyzer_status", ""))
    reason = str(final_result.get("reason", ""))
    completed_at = str(final_result.get("completed_at", ""))
    last_out_path = final_result.get("last_out_path")
    skipped_execution = bool(final_result.get("skipped_execution", False))
    return {
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(selected_inp),
        "run_id": str(state.get("run_id", "")),
        "status": final_status,
        "analyzer_status": analyzer_status,
        "reason": reason,
        "attempt_count": len(attempts) if isinstance(attempts, list) else 0,
        "completed_at": completed_at,
        "last_out_path": last_out_path if isinstance(last_out_path, str) else None,
        "resumed": bool(final_result.get("resumed", False)),
        "skipped_execution": skipped_execution,
    }


def finished_notification_already_sent(state: Mapping[str, Any]) -> bool:
    final_result = state.get("final_result")
    if not isinstance(final_result, Mapping):
        return False
    return bool(str(final_result.get("finished_notification_sent_at") or "").strip())


def run_started_message(event: RunStartedNotification) -> Message:
    reaction_dir = Path(event["reaction_dir"])
    current_inp = Path(event["current_inp"])
    status = str(event["status"]).strip().lower()
    resumed = bool(event.get("resumed"))
    title = "ORCA resumed" if resumed else "ORCA started"

    fields = [
        field_row("Job", text(reaction_dir.name or reaction_dir.as_posix())),
        field_row(
            "Attempt",
            raw(f"#{event['attempt_index']} ("),
            code(status or RunStatus.RUNNING.value),
            raw(")"),
        ),
        field_row("Input", code(current_inp.name)),
    ]
    if resumed:
        fields.append(field_row("Mode", text("resumed run")))
    fields.append(field_row("Directory", code(event["reaction_dir"])))
    return Message(
        title=title,
        severity="info",
        groups=(group(*fields),),
        author="orca_auto",
    )


def run_finished_message(event: RunFinishedNotification) -> Message:
    reaction_dir = Path(event["reaction_dir"])
    status = str(event["status"]).strip().lower()
    title = {
        RunStatus.COMPLETED.value: "ORCA completed",
        RunStatus.CANCELLED.value: "ORCA cancelled",
    }.get(status, "ORCA failed")
    if status == RunStatus.COMPLETED.value:
        severity: Severity = "success"
    elif status == RunStatus.CANCELLED.value:
        severity = "warning"
    else:
        severity = "error"
    status_text = status or "unknown"

    fields = [
        field_row("Job", text(reaction_dir.name or reaction_dir.as_posix())),
        field_row("Result", code(status_text)),
        field_row("Attempts", text(event["attempt_count"])),
        field_row("Reason", code(event["reason"])),
        field_row("Analyzer", code(event["analyzer_status"])),
    ]
    last_out_path = event.get("last_out_path")
    if isinstance(last_out_path, str) and last_out_path.strip():
        fields.append(field_row("Output", code(Path(last_out_path).name)))
    if event.get("skipped_execution"):
        fields.append(field_row("Mode", text("reused existing output")))
    elif event.get("resumed"):
        fields.append(field_row("Mode", text("resumed run")))
    fields.append(field_row("Directory", code(event["reaction_dir"])))
    return Message(
        title=title,
        severity=severity,
        groups=(group(*fields),),
        author="orca_auto",
    )


def queue_enqueued_message(event: QueueEnqueuedNotification) -> Message:
    reaction_dir = Path(event["reaction_dir"])
    fields = [
        field_row("Job", text(reaction_dir.name or reaction_dir.as_posix())),
        field_row("Queue ID", code(event["queue_id"])),
        field_row("Priority", text(event["priority"])),
    ]
    if event.get("force"):
        fields.append(field_row("Mode", text("force re-enqueue")))
    fields.append(field_row("Directory", code(event["reaction_dir"])))
    return Message(
        title="ORCA queued",
        severity="info",
        groups=(group(*fields),),
        author="orca_auto",
    )


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def notify_run_started_event(channel: MessageChannel, event: RunStartedNotification) -> bool:
    if not channel.enabled:
        logger.debug("run_started_notification_disabled")
        return False
    sent = channel.send(run_started_message(event)).sent
    _log_delivery(
        "run_started", sent, reaction_dir=event["reaction_dir"], attempt=event["attempt_index"]
    )
    return sent


def notify_run_finished_event(channel: MessageChannel, event: RunFinishedNotification) -> bool:
    if not channel.enabled:
        logger.debug("run_finished_notification_disabled")
        return False
    sent = channel.send(run_finished_message(event)).sent
    _log_delivery("run_finished", sent, reaction_dir=event["reaction_dir"], status=event["status"])
    return sent


def notify_queue_enqueued_event(channel: MessageChannel, event: QueueEnqueuedNotification) -> bool:
    if not channel.enabled:
        logger.debug("queue_enqueued_notification_disabled")
        return False
    sent = channel.send(queue_enqueued_message(event)).sent
    _log_delivery(
        "queue_enqueued", sent, queue_id=event["queue_id"], reaction_dir=event["reaction_dir"]
    )
    return sent


def _log_delivery(kind: str, sent: bool, **context: object) -> None:
    detail = " ".join(f"{key}={value}" for key, value in context.items())
    if sent:
        logger.info("%s_notification_sent: %s", kind, detail)
    else:
        logger.warning("%s_notification_failed: %s", kind, detail)


__all__ = [
    "dispatch_notification",
    "notification_channel",
    "notify_queue_enqueued_event",
    "notify_run_finished_event",
    "notify_run_started_event",
    "queue_enqueued_message",
    "run_finished_message",
    "run_started_message",
]
