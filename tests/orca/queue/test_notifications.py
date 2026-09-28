"""``orca_auto.orca.queue.notifications``: the parent's queued and terminal claims."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from orca_auto.core.admission import admission_dir
from orca_auto.core.messaging.channel import SendResult
from orca_auto.core.queue import persistence, store
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
)
from orca_auto.core.queue.types import QueueStatus
from orca_auto.core.queue.worker.loop import QueueWorkerLoop
from orca_auto.orca import notifications as orca_notifications
from orca_auto.orca import submission
from orca_auto.orca.config import AppConfig, load_config
from orca_auto.orca.queue import adapter, job_records, notifications
from orca_auto.orca.queue.entries import QUEUED_NOTIFICATION_PENDING_KEY
from orca_auto.orca.queue.worker import OrcaQueueWorker
from orca_auto.orca.state import new_state, save_state
from orca_auto.orca.state_reading import load_state, state_path
from orca_auto.orca.types import RunState
from tests.conftest import (
    RecordingChannel,
    claim_next_entry,
    make_app_cfg,
    make_queue_entry,
)
from tests.orca.test_submission import _real_submission
from tests.queue_worker_helpers import write_completed_run_state


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
    result = submission.submit_reaction_dir_to_queue(args, cfg=load_config(args.config))
    assert result.status == "submitted"
    assert not entered.is_set()  # CLI only hands off a durable intent.
    cfg = load_config(args.config)
    worker = OrcaQueueWorker(cfg, config_path=args.config)
    reserved = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            future = pool.submit(worker._admit_next)
            assert entered.wait(5)
            status, reserved = future.result(timeout=5)
            assert status == "processed" and reserved is not None
            assert not release.is_set()
            current = adapter.list_queue(tmp_path)[0]
            assert current.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
            assert current.metadata[QUEUED_NOTIFICATION_PENDING_KEY] is False
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
            QUEUED_NOTIFICATION_PENDING_KEY: True,
        },
    )
    persistence.save_entries(tmp_path, [entry])
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
            QUEUED_NOTIFICATION_PENDING_KEY: True,
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
    persistence.save_entries(tmp_path, [ready, waiting, cancelled, old])
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


# ---------------------------------------------------------------------------
# Terminal claim
# ---------------------------------------------------------------------------


def test_slow_notification_does_not_block_loop_or_write_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_completed_run_state(tmp_path)
    entered, release, delivered = threading.Event(), threading.Event(), threading.Event()
    events: list[str] = []

    class Channel:
        enabled = True

        def send(self, _message: object) -> SendResult:
            events.append("send")
            entered.set()
            assert release.wait(30)
            delivered.set()
            return SendResult(sent=True)

    monkeypatch.setattr(notifications, "notification_channel", lambda *_args, **_kwargs: Channel())

    class Loop(QueueWorkerLoop):
        def __init__(self) -> None:
            super().__init__(max_concurrent=2, poll_interval_seconds=0, sleep_fn=lambda _: None)

        def _check_completed_jobs(self, **_kwargs: object) -> None:
            notifications.claim_and_send_terminal(
                AppConfig(), str(tmp_path), expected_job_id="task_terminal_123"
            )
            events.append("release_slot")

        def _check_cancel_requests(self) -> None:
            events.append("cancel_pass")

        def _fill_slots(self, **_kwargs: object) -> str:
            events.append("admit")
            return "idle"

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(Loop().run_pass)
        try:
            assert entered.wait(30)
            future.result(timeout=30)
            assert [event for event in events if event != "send"] == [
                "release_slot",
                "cancel_pass",
                "admit",
            ]
            # A reconstructed owner must see the durable claim during delivery.
            assert not notifications.claim_and_send_terminal(
                AppConfig(), str(tmp_path), expected_job_id="task_terminal_123"
            )
            successor = new_state(tmp_path, tmp_path / "rxn.inp")
            successor["job_id"] = "successor"
            save_state(tmp_path, successor)
            before = state_path(tmp_path).read_bytes()
        finally:
            release.set()
        assert delivered.wait(1)
        # Wait until the transport finally block has also released its slot.
        for thread in threading.enumerate():
            if thread.name == "orca-terminal-notification":
                thread.join(timeout=1)
        assert state_path(tmp_path).read_bytes() == before
        assert events.count("send") == 1


@pytest.mark.parametrize("after_commit", [False, True])
def test_ambiguous_notification_claim_never_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_channel: RecordingChannel,
    after_commit: bool,
) -> None:
    write_completed_run_state(tmp_path)
    sends = recording_channel.sends

    def fail_save(path: Path, state: RunState) -> None:
        if after_commit:
            save_state(path, state)
        raise OSError("claim durability failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(notifications, "save_state", fail_save)
        with pytest.raises(OSError, match="claim durability"):
            notifications.claim_and_send_terminal(
                AppConfig(), str(tmp_path), expected_job_id="task_terminal_123"
            )
    assert sends == []
    if after_commit:
        assert not notifications.claim_and_send_terminal(AppConfig(), str(tmp_path))
        assert sends == []
        state = load_state(tmp_path)
        assert (
            state
            and state["final_result"]
            and state["final_result"]["finished_notification_claimed_at"]
        )


def test_wrong_run_claim_does_not_write_or_send(
    tmp_path: Path, recording_channel: RecordingChannel
) -> None:
    write_completed_run_state(tmp_path)
    before = state_path(tmp_path).read_bytes()
    assert not notifications.claim_and_send_terminal(
        AppConfig(), str(tmp_path), expected_job_id="task_terminal_123", expected_run_id="successor"
    )
    assert state_path(tmp_path).read_bytes() == before
    assert recording_channel.sends == []


def test_bounded_sends_recover_capacity_after_transport_and_start_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release, entered = threading.Event(), threading.Event()
    guard = threading.Lock()
    sends = []
    slots = threading.BoundedSemaphore(4)
    monkeypatch.setattr(orca_notifications, "_NOTIFICATION_SLOTS", slots)

    class Channel:
        enabled = True

        def send(self, message: object) -> SendResult:
            with guard:
                sends.append(message)
                if len(sends) == 4:
                    entered.set()
            assert release.wait(30)
            raise RuntimeError("synthetic transport failure")

    monkeypatch.setattr(notifications, "notification_channel", lambda *_args, **_kwargs: Channel())
    roots = [tmp_path / str(i) for i in range(6)]
    for root in roots:
        root.mkdir()
        write_completed_run_state(root)
    try:
        for root in roots[:4]:
            assert notifications.claim_and_send_terminal(AppConfig(), str(root))
        assert entered.wait(30)
        assert not notifications.claim_and_send_terminal(AppConfig(), str(roots[4]))
        assert len(sends) == 4
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == "orca-terminal-notification":
                thread.join(timeout=1)
    # Saturation is an advisory missed delivery, not a retry on restart.
    assert not notifications.claim_and_send_terminal(AppConfig(), str(roots[4]))
    with monkeypatch.context() as patcher:

        def start_failure(_thread: threading.Thread) -> None:
            raise RuntimeError("synthetic thread start failure")

        patcher.setattr(threading.Thread, "start", start_failure)
        assert not notifications.claim_and_send_terminal(AppConfig(), str(roots[5]))
    assert [slots.acquire(blocking=False) for _ in range(5)] == [True] * 4 + [False]
    for _ in range(4):
        slots.release()


def test_real_finalizer_releases_admission_slot_while_delivery_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from orca_auto.core.admission import list_slots, reserve_slot
    from orca_auto.core.queue.types import QueueStatus
    from orca_auto.orca.config import OrcaRuntimeConfig
    from orca_auto.orca.queue import adapter
    from orca_auto.orca.queue.models import OrcaRunningJob as _RunningJob
    from orca_auto.orca.queue.worker import OrcaQueueWorker

    entered, release = threading.Event(), threading.Event()

    class Channel:
        enabled = True

        def send(self, _message: object) -> SendResult:
            entered.set()
            assert release.wait(10)
            return SendResult(sent=True)

    monkeypatch.setattr(notifications, "notification_channel", lambda *_args, **_kwargs: Channel())
    monkeypatch.setattr(job_records, "upsert_terminal_job_record", lambda *_args, **_kwargs: True)
    cfg = AppConfig(runtime=OrcaRuntimeConfig(allowed_root=str(tmp_path), max_concurrent=2))
    worker = OrcaQueueWorker(cfg, str(tmp_path / "config.yaml"))
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    write_completed_run_state(job_dir)
    entry = adapter.enqueue(tmp_path, str(job_dir), task_id="task_terminal_123")
    claim_next_entry(tmp_path)
    token = reserve_slot(
        admission_dir(tmp_path),
        2,
        work_dir=str(job_dir),
        queue_id=entry.queue_id,
        source="queue_worker",
        state="reserved",
    )
    assert token
    job = _RunningJob(
        queue_root=tmp_path,
        queue_id=entry.queue_id,
        reaction_dir=str(job_dir),
        process=MagicMock(),
        admission_token=token,
        task_id=entry.task_id,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker._finalize_completed_job, entry.queue_id, job, rc=0)
        try:
            assert entered.wait(5)
            future.result(timeout=5)
            assert len(list_slots(admission_dir(tmp_path))) == 0
            [completed] = adapter.list_queue(tmp_path)
            assert completed.status == QueueStatus.COMPLETED
            assert completed.metadata.get("orca_terminal_replay") is None
        finally:
            release.set()
    for thread in threading.enumerate():
        if thread.name == "orca-terminal-notification":
            thread.join(timeout=1)
