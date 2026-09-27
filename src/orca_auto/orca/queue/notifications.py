"""The parent's two notification claims, each claimed once before delivery.

``notify_queued_jobs`` claims the queued message from the durable queue row;
``claim_and_send_terminal`` claims the terminal message in the generation's
``job_state.json``. Delivery is advisory and never writes state afterward.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from functools import partial
from pathlib import Path

from orca_auto.core.queue.publication import QUEUE_RECORD_SYNC_COMPLETE, queue_record_sync_state
from orca_auto.core.queue.store import mutate_entries
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.statuses import TERMINAL_STATUSES, normalize_status

from ..config import AppConfig
from ..notifications import (
    build_run_finished_notification,
    dispatch_notification,
    finished_notification_already_sent,
    notification_channel,
    notify_queue_enqueued_event,
    notify_run_finished_event,
)
from ..run_lock import acquire_run_lock
from ..state import now_utc_iso, save_state
from ..state_reading import load_state, payload_matches_expected_job_id
from ..types import QueueEnqueuedNotification
from .entries import (
    QUEUED_NOTIFICATION_PENDING_KEY,
    is_orca_queue_entry,
    queue_entry_force,
    queue_entry_reaction_dir,
)
from .roots import queue_root

logger = logging.getLogger(__name__)


def _claim_queued_notifications(root: Path) -> list[QueueEnqueuedNotification]:
    def claim(entries: list[QueueEntry]) -> tuple[list[QueueEnqueuedNotification], bool]:
        notifications: list[QueueEnqueuedNotification] = []
        for index, entry in enumerate(entries):
            if (
                not is_orca_queue_entry(entry)
                or entry.status != QueueStatus.PENDING
                or entry.cancel_requested
                or queue_record_sync_state(entry) != QUEUE_RECORD_SYNC_COMPLETE
                or entry.metadata.get(QUEUED_NOTIFICATION_PENDING_KEY) is not True
            ):
                continue
            notifications.append(
                {
                    "queue_id": entry.queue_id,
                    "reaction_dir": queue_entry_reaction_dir(entry),
                    "priority": entry.priority,
                    "force": queue_entry_force(entry),
                    "enqueued_at": entry.enqueued_at,
                }
            )
            entries[index] = replace(
                entry,
                metadata={
                    **entry.metadata,
                    QUEUED_NOTIFICATION_PENDING_KEY: False,
                },
            )
        return notifications, bool(notifications)

    return mutate_entries(root, claim)


def notify_queued_jobs(cfg: AppConfig) -> None:
    channel = notification_channel(cfg)
    if not channel.enabled:
        return
    root = queue_root(cfg)
    try:
        notifications = _claim_queued_notifications(root)
    except Exception:  # An ambiguous claim must never send or gate admission.
        logger.exception("Queued notification claim failed: %s", root)
        return
    for event in notifications:
        dispatch_notification(partial(notify_queue_enqueued_event, channel, event), kind="queued")


def claim_and_send_terminal(
    cfg: AppConfig,
    reaction_dir: str,
    *,
    expected_job_id: str | None = None,
    expected_run_id: str | None = None,
) -> bool:
    """Claim one best-effort notification, then dispatch an immutable message.

    A crash or saturated sender after the claim may lose an advisory message.
    Transport never writes state: the job directory may already hold a successor.
    """
    channel = notification_channel(cfg)
    if not channel.enabled:
        return False

    job_dir = Path(reaction_dir).expanduser().resolve()
    with acquire_run_lock(job_dir):
        state = load_state(job_dir)
        if not state or not payload_matches_expected_job_id(state, expected_job_id):
            return False
        if not state.get("run_id") or (expected_run_id and state.get("run_id") != expected_run_id):
            return False
        final_result = state.get("final_result")
        if (
            normalize_status(state.get("status")) not in TERMINAL_STATUSES
            or not isinstance(final_result, dict)
            or finished_notification_already_sent(state)
            or final_result.get("finished_notification_claimed_at")
        ):
            return False
        selected_inp_text = str(state.get("selected_inp") or "").strip()
        notification = build_run_finished_notification(
            reaction_dir=job_dir,
            selected_inp=Path(selected_inp_text) if selected_inp_text else job_dir / "-",
            state=state,
            status=str(final_result.get("status") or state.get("status") or "").strip(),
            final_result=final_result,
        )
        final_result["finished_notification_claimed_at"] = now_utc_iso()
        save_state(job_dir, state)
    return dispatch_notification(
        lambda: notify_run_finished_event(channel, notification), kind="terminal"
    )
