from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from orca_auto.core.messaging.channel import SendResult
from orca_auto.core.queue.worker.loop import QueueWorkerLoop
from orca_auto.orca.config import AppConfig
from orca_auto.orca.queue import worker_tracking
from orca_auto.orca.state import new_state, save_state
from orca_auto.orca.state_reading import load_state, state_path
from orca_auto.orca.types import RunState
from tests.queue_worker_helpers import write_completed_run_state


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

    monkeypatch.setattr(worker_tracking, "build_channel", lambda *_args, **_kwargs: Channel())

    class Loop(QueueWorkerLoop):
        def __init__(self) -> None:
            super().__init__(max_concurrent=2, poll_interval_seconds=0, sleep_fn=lambda _: None)

        def _check_completed_jobs(self, **_kwargs: object) -> None:
            worker_tracking.notify_terminal_job_from_state(
                AppConfig(), str(tmp_path), expected_job_id="task_terminal_123"
            )
            events.append("release_slot")

        def _check_cancel_requests(self) -> None:
            events.append("cancel_pass")

        def _fill_slots(self, **_kwargs: object) -> str:
            events.append("admit")
            return "idle"

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(Loop()._run_iteration)
        try:
            assert entered.wait(30)
            future.result(timeout=30)
            assert [event for event in events if event != "send"] == [
                "release_slot",
                "cancel_pass",
                "admit",
            ]
            # A reconstructed owner must see the durable claim during delivery.
            assert not worker_tracking.notify_terminal_job_from_state(
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_commit: bool
) -> None:
    write_completed_run_state(tmp_path)
    sends: list[object] = []

    class Channel:
        enabled = True

        def send(self, message: object) -> SendResult:
            sends.append(message)
            return SendResult(sent=True)

    monkeypatch.setattr(worker_tracking, "build_channel", lambda *_args, **_kwargs: Channel())

    def fail_save(path: Path, state: RunState) -> None:
        if after_commit:
            save_state(path, state)
        raise OSError("claim durability failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(worker_tracking, "save_state", fail_save)
        with pytest.raises(OSError, match="claim durability"):
            worker_tracking.notify_terminal_job_from_state(
                AppConfig(), str(tmp_path), expected_job_id="task_terminal_123"
            )
    assert sends == []
    if after_commit:
        assert not worker_tracking.notify_terminal_job_from_state(AppConfig(), str(tmp_path))
        assert sends == []
        state = load_state(tmp_path)
        assert (
            state
            and state["final_result"]
            and state["final_result"]["finished_notification_claimed_at"]
        )


def test_wrong_run_claim_does_not_write_or_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_completed_run_state(tmp_path)
    before = state_path(tmp_path).read_bytes()
    monkeypatch.setattr(
        worker_tracking,
        "build_channel",
        lambda *_args, **_kwargs: type("Channel", (), {"enabled": True})(),
    )
    assert not worker_tracking.notify_terminal_job_from_state(
        AppConfig(), str(tmp_path), expected_job_id="task_terminal_123", expected_run_id="successor"
    )
    assert state_path(tmp_path).read_bytes() == before


def test_bounded_sends_recover_capacity_after_transport_and_start_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release, entered = threading.Event(), threading.Event()
    guard = threading.Lock()
    sends = []
    slots = threading.BoundedSemaphore(4)
    monkeypatch.setattr(worker_tracking, "_NOTIFICATION_SLOTS", slots)

    class Channel:
        enabled = True

        def send(self, message: object) -> SendResult:
            with guard:
                sends.append(message)
                if len(sends) == 4:
                    entered.set()
            assert release.wait(30)
            raise RuntimeError("synthetic transport failure")

    monkeypatch.setattr(worker_tracking, "build_channel", lambda *_args, **_kwargs: Channel())
    roots = [tmp_path / str(i) for i in range(6)]
    for root in roots:
        root.mkdir()
        write_completed_run_state(root)
    try:
        for root in roots[:4]:
            assert worker_tracking.notify_terminal_job_from_state(AppConfig(), str(root))
        assert entered.wait(30)
        assert not worker_tracking.notify_terminal_job_from_state(AppConfig(), str(roots[4]))
        assert len(sends) == 4
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == "orca-terminal-notification":
                thread.join(timeout=1)
    # Saturation is an advisory missed delivery, not a retry on restart.
    assert not worker_tracking.notify_terminal_job_from_state(AppConfig(), str(roots[4]))
    with monkeypatch.context() as patcher:

        def start_failure(_thread: threading.Thread) -> None:
            raise RuntimeError("synthetic thread start failure")

        patcher.setattr(threading.Thread, "start", start_failure)
        assert not worker_tracking.notify_terminal_job_from_state(AppConfig(), str(roots[5]))
    assert [slots.acquire(blocking=False) for _ in range(5)] == [True] * 4 + [False]
    for _ in range(4):
        slots.release()


def test_real_finalizer_releases_admission_slot_while_delivery_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from orca_auto.core.admission import active_slot_count, reserve_slot
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

    monkeypatch.setattr(worker_tracking, "build_channel", lambda *_args, **_kwargs: Channel())
    monkeypatch.setattr(
        worker_tracking, "upsert_terminal_job_record", lambda *_args, **_kwargs: True
    )
    cfg = AppConfig(runtime=OrcaRuntimeConfig(allowed_root=str(tmp_path)))
    worker = OrcaQueueWorker(cfg, str(tmp_path / "config.yaml"), max_concurrent=2)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    write_completed_run_state(job_dir)
    entry = adapter.enqueue(tmp_path, str(job_dir), task_id="task_terminal_123")
    adapter.dequeue_next(tmp_path)
    token = reserve_slot(
        tmp_path,
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
            assert active_slot_count(tmp_path) == 0
            [completed] = adapter.list_queue(tmp_path)
            assert completed.status == adapter.QueueStatus.COMPLETED
            assert completed.metadata.get("orca_terminal_replay") is None
        finally:
            release.set()
    for thread in threading.enumerate():
        if thread.name == "orca-terminal-notification":
            thread.join(timeout=1)
