"""Parent-owned queued notifications, claimed from the durable queue once."""

from __future__ import annotations

import logging
from dataclasses import replace
from functools import partial
from pathlib import Path

from orca_auto.core.queue.publication import QUEUE_RECORD_SYNC_COMPLETE, queue_record_sync_state
from orca_auto.core.queue.store import mutate_entries
from orca_auto.core.queue.types import QueueEntry, QueueStatus

from ..config import AppConfig
from ..notifications import dispatch_notification, notification_channel, notify_queue_enqueued_event
from ..types import QueueEnqueuedNotification
from .entries import queue_entry_force, queue_entry_reaction_dir
from .identity import entry_matches_engine_identity
from .roots import queue_roots

logger = logging.getLogger(__name__)
QUEUED_NOTIFICATION_PENDING_KEY = "orca_queued_notification_pending"


def _claim_queued_notifications(root: Path) -> list[QueueEnqueuedNotification]:
    def claim(entries: list[QueueEntry]) -> tuple[list[QueueEnqueuedNotification], bool]:
        notifications: list[QueueEnqueuedNotification] = []
        for index, entry in enumerate(entries):
            if (
                not entry_matches_engine_identity(entry, "orca")
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
    for root in queue_roots(cfg):
        try:
            notifications = _claim_queued_notifications(root)
        except Exception:  # An ambiguous claim must never send or gate admission.
            logger.exception("Queued notification claim failed: %s", root)
            continue
        for event in notifications:
            dispatch_notification(
                partial(notify_queue_enqueued_event, channel, event), kind="queued"
            )
