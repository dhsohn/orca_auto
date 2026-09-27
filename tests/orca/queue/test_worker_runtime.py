from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from orca_auto.core.queue.store import QueueLockTimeoutError
from orca_auto.core.queue.types import QueueEntry
from orca_auto.orca.queue import worker as worker_mod
from orca_auto.orca.queue.models import OrcaRunningJob
from orca_auto.orca.queue.worker import OrcaQueueWorker
from tests.conftest import make_app_cfg
from tests.process_helpers import FakeManagedProcess


def _worker(root: Path) -> OrcaQueueWorker:
    return OrcaQueueWorker(make_app_cfg(str(root)), str(root / "config.yaml"))


def _job(root: Path, queue_id: str, *, exited: bool = False) -> OrcaRunningJob:
    return OrcaRunningJob(
        queue_root=root,
        queue_id=queue_id,
        reaction_dir=str(root / queue_id),
        process=FakeManagedProcess(poll_result=0 if exited else None),
        admission_token="slot-" + queue_id,
        task_id="task-" + queue_id,
    )


def test_running_job_retains_queue_generation_and_process(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    process = FakeManagedProcess()
    entry = QueueEntry(
        "queue-1",
        "orca_auto_orca",
        "task-1",
        "orca_run_inp",
        "orca",
        metadata={"reaction_dir": str(tmp_path / "run")},
    )
    job = worker._make_running_job(
        queue_root=tmp_path, entry=entry, process=process, admission_token="slot-1"
    )
    assert (job.queue_root, job.queue_id, job.task_id, job.reaction_dir) == (
        tmp_path,
        "queue-1",
        "task-1",
        str(tmp_path / "run"),
    )
    assert job.process is process
    assert job.admission_token == "slot-1"


def test_cancel_requests_discard_only_successfully_cancelled_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _worker(tmp_path)
    worker._running = {qid: _job(tmp_path, qid) for qid in ("1", "2")}
    cancelled: list[str] = []
    checks: list[tuple[Path, Mapping[str, str | None]]] = []

    def requested(root: Path, tasks: Mapping[str, str | None]) -> set[str]:
        checks.append((root, tasks))
        return {"1"}

    def cancel(qid: str, _job: OrcaRunningJob) -> bool:
        cancelled.append(qid)
        return True

    monkeypatch.setattr(worker_mod, "cancel_requested_ids", requested)
    monkeypatch.setattr(worker, "_cancel_running_job", cancel)
    worker._check_cancel_requests()
    assert cancelled == ["1"]
    assert list(worker._running) == ["2"]
    assert checks == [(tmp_path, {"1": "task-1", "2": "task-2"})]


def test_cancel_requests_never_signal_retained_completed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _worker(tmp_path)
    worker._running = {"1": _job(tmp_path, "1", exited=True)}
    monkeypatch.setattr(
        worker_mod,
        "cancel_requested_ids",
        lambda *_args: pytest.fail("completed child must not be considered for cancellation"),
    )
    worker._check_cancel_requests()
    assert list(worker._running) == ["1"]


def test_busy_root_does_not_delay_cancellation_at_another_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _worker(tmp_path)
    worker._running = {str(i): _job(tmp_path / str(i // 2), str(i)) for i in range(4)}
    cancelled: list[str] = []
    checks: list[Path] = []

    def requested(root: Path, _tasks: Mapping[str, str | None]) -> set[str]:
        checks.append(root)
        if root == tmp_path / "0":
            raise QueueLockTimeoutError("busy")
        return {"2", "3"}

    def cancel(qid: str, _job: OrcaRunningJob) -> bool:
        cancelled.append(qid)
        return qid == "2"

    monkeypatch.setattr(worker_mod, "cancel_requested_ids", requested)
    monkeypatch.setattr(worker, "_cancel_running_job", cancel)
    worker._check_cancel_requests()
    assert checks == [tmp_path / "0", tmp_path / "1"]
    assert cancelled == ["2", "3"]
    assert list(worker._running) == ["0", "1", "3"]


def test_replay_state_is_initialized_once_and_is_owned_by_each_worker(tmp_path: Path) -> None:
    first, second = _worker(tmp_path), _worker(tmp_path)
    first.replay_state.blocked_marker_keys.add(("root", "queue"))
    assert second.replay_state.blocked_marker_keys == set()
    assert first.replay_state.reconcile_statuses is None


def test_injected_process_and_sleep_are_used_by_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_app_cfg(str(tmp_path))
    process = FakeManagedProcess()
    starts: list[dict[str, object]] = []
    sleeps: list[float] = []

    def start(**kwargs: object) -> FakeManagedProcess:
        starts.append(kwargs)
        return process

    worker = OrcaQueueWorker(cfg, "config.yaml", sleep_fn=sleeps.append)
    monkeypatch.setattr(worker, "_start_background_process", start)
    monkeypatch.setattr(worker, "_on_worker_process_started", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(worker, "_reconcile_worker_state", lambda: None)
    entry = QueueEntry(
        "q",
        "orca_auto_orca",
        "task",
        "orca_run_inp",
        "orca",
        metadata={"reaction_dir": str(tmp_path)},
    )
    assert worker._start_job(tmp_path, entry, admission_token="slot")
    worker._sleep()
    assert worker._running["q"].process is process
    assert starts == [
        {
            "queue_root": tmp_path,
            "entry": entry,
            "admission_token": "slot",
        }
    ]
    assert sleeps == [worker.poll_interval_seconds]


def test_running_identity_prevents_reclaiming_normalized_queue_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _worker(tmp_path)
    process = FakeManagedProcess()
    monkeypatch.setattr(worker, "_start_background_process", lambda **_kwargs: process)
    monkeypatch.setattr(worker, "_on_worker_process_started", lambda *_args, **_kwargs: True)
    entry = QueueEntry(
        " queue-1 ",
        "orca_auto_orca",
        "task-1",
        "orca_run_inp",
        "orca",
        metadata={"reaction_dir": str(tmp_path)},
    )
    assert worker._start_job(tmp_path, entry, admission_token="slot-1")
    assert list(worker._running) == ["queue-1"]
    assert worker._running["queue-1"].queue_id == "queue-1"
    assert worker._skip_entry(entry)
    assert worker._skip_entry(replace(entry, queue_id="queue-1"))
