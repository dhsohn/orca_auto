from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from orca_auto.core.messaging.channel import SendResult
from orca_auto.core.queue import store
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
)
from orca_auto.core.queue.types import QueueStatus
from orca_auto.orca import submission
from orca_auto.orca.queue import adapter, notifications
from orca_auto.orca.queue.worker import OrcaQueueWorker
from tests.conftest import RecordingChannel, make_app_cfg, make_queue_entry
from tests.test_run_inp_submission import _real_submission


def _join_senders() -> None:
    for thread in threading.enumerate():
        if thread.name == "orca-queued-notification":
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_slow_queued_delivery_does_not_delay_submission_or_reservation(tmp_path, monkeypatch):
    _root, args = _real_submission(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    sends = []

    class Channel:
        enabled = True

        def send(self, message):
            sends.append(message)
            entered.set()
            assert release.wait(10)
            return SendResult(sent=True)

    monkeypatch.setattr(notifications, "notification_channel", lambda _cfg: Channel())
    # Restore real delivery; the submission fixture replaces the old messenger seam.
    from orca_auto.orca.notifications import notify_queue_enqueued_event

    monkeypatch.setattr(notifications, "notify_queue_enqueued_event", notify_queue_enqueued_event)
    result = submission.submit_reaction_dir_to_queue(args)
    assert result.status == "submitted"
    assert not entered.is_set()  # CLI only hands off a durable intent.
    cfg = submission.load_config(args.config)
    worker = OrcaQueueWorker(cfg, config_path=args.config)
    reserved = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            future = pool.submit(worker._reserve_next_entry)
            assert entered.wait(5)
            status, reserved = future.result(timeout=5)
            assert status == "processed" and reserved is not None
            assert not release.is_set()
            current = adapter.list_queue(tmp_path)[0]
            assert current.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
            assert current.metadata[notifications.QUEUED_NOTIFICATION_PENDING_KEY] is False
            notifications.notify_queued_jobs(cfg)  # A replacement owner cannot claim it twice.
            assert len(sends) == 1
        finally:
            release.set()
            _join_senders()
            if reserved is not None:
                worker._release_admission_slot(reserved.admission_token)


@pytest.mark.parametrize("after_commit", [False, True])
def test_ambiguous_queued_claim_never_sends(tmp_path, monkeypatch, after_commit):
    entry = make_queue_entry(
        reaction_dir=tmp_path / "job",
        metadata={
            notifications.QUEUED_NOTIFICATION_PENDING_KEY: True,
        },
    )
    store.save_entries(tmp_path, [entry])
    channel = RecordingChannel()
    monkeypatch.setattr(notifications, "notification_channel", lambda _cfg: channel)

    def fail_claim(root, callback):
        if after_commit:
            store.mutate_entries(root, callback)
        raise OSError("synthetic ambiguous queue save")

    with monkeypatch.context() as patcher:
        patcher.setattr(notifications, "mutate_entries", fail_claim)
        notifications.notify_queued_jobs(make_app_cfg(tmp_path))
    assert channel.sends == []
    notifications.notify_queued_jobs(make_app_cfg(tmp_path))
    _join_senders()
    assert len(channel.sends) == (0 if after_commit else 1)


def test_queued_delivery_waits_for_publication_and_ignores_cancelled_or_old_rows(
    tmp_path, monkeypatch
):
    channel = RecordingChannel()
    monkeypatch.setattr(notifications, "notification_channel", lambda _cfg: channel)
    ready = make_queue_entry(
        reaction_dir=tmp_path / "ready",
        metadata={
            notifications.QUEUED_NOTIFICATION_PENDING_KEY: True,
        },
    )
    waiting = replace(
        ready,
        queue_id="waiting",
        metadata={
            **ready.metadata,
            QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_REPAIR_PENDING,
        },
    )
    cancelled = replace(ready, queue_id="cancelled", status=QueueStatus.CANCELLED)
    old = make_queue_entry(reaction_dir=tmp_path / "old")
    store.save_entries(tmp_path, [ready, waiting, cancelled, old])
    cfg = make_app_cfg(tmp_path)
    notifications.notify_queued_jobs(cfg)
    _join_senders()
    assert len(channel.sends) == 1
    assert adapter.update_metadata(
        tmp_path, "waiting", {QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_COMPLETE}
    )
    notifications.notify_queued_jobs(cfg)
    _join_senders()
    assert len(channel.sends) == 2
    notifications.notify_queued_jobs(cfg)
    _join_senders()
    assert len(channel.sends) == 2
