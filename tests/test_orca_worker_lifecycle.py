"""ORCA lifecycle contracts after removing generic callback mediation.

The worker runs against a real queue file and a real admission store under
``tmp_path``; the outcomes pinned are queue rows, admission slots and
``job_locations.json``. ``terminate_process_group`` stays stubbed because the
real one signals a POSIX process group.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.admission import get_slot, prepare_slot_engine_process, reserve_slot
from orca_auto.core.queue.store import save_entries as save_entries_core
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.config import AppConfig
from orca_auto.orca.job_locations import list_job_location_records
from orca_auto.orca.queue import replay
from orca_auto.orca.queue import worker as worker_mod
from orca_auto.orca.queue.adapter import QUEUE_FILE_NAME, cancel, list_queue
from orca_auto.orca.queue.models import OrcaRunningJob
from orca_auto.orca.queue.terminal_replay import terminal_replay_marker_from_entry
from orca_auto.orca.queue.worker import OrcaQueueWorker
from orca_auto.orca.statuses import RunStatus
from tests.conftest import ProcessIdentity, enqueue_entry, make_queue_entry, write_run_state
from tests.process_helpers import FakeManagedProcess


def _entry(root: Path, **fields: Any) -> QueueEntry:
    fields.setdefault("status", QueueStatus.RUNNING)
    fields.setdefault("task_id", "task-1")
    return make_queue_entry(queue_id="queue-1", reaction_dir=root / "reaction", **fields)


def _job(root: Path, *, admission_token: str = "slot-1", task_id: str | None = "task-1") -> Any:
    return OrcaRunningJob(
        queue_root=root,
        queue_id="queue-1",
        reaction_dir=str(root / "reaction"),
        task_id=task_id,
        process=FakeManagedProcess(poll_result=0),
        admission_token=admission_token,
    )


def _row(root: Path, queue_id: str) -> QueueEntry | None:
    return next((entry for entry in list_queue(root) if entry.queue_id == queue_id), None)


def _reserve(root: Path, **fields: Any) -> str:
    token = reserve_slot(root, 4, source="queue_worker", state="reserved", **fields)
    assert token
    return token


def _corrupt_index(root: Path) -> None:
    (root / "job_locations.json").write_text("{not a job location index", encoding="utf-8")


@pytest.fixture
def worker(tmp_path: Path, app_cfg: Callable[..., AppConfig]) -> OrcaQueueWorker:
    return OrcaQueueWorker(app_cfg(runs_root=tmp_path), "config.yaml")


@pytest.mark.parametrize("metadata_key", ["job_dir", "reaction_dir"])
def test_attach_preserves_admission_identity_and_work_dir(
    tmp_path: Path,
    worker: OrcaQueueWorker,
    stable_process_identity: ProcessIdentity,
    metadata_key: str,
) -> None:
    # The child pid the slot is handed over to must be verifiable as a process.
    reaction = tmp_path / "reaction"
    reaction.mkdir()
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    base = _entry(tmp_path)
    if metadata_key == "job_dir":
        # An explicit job_dir wins over the reaction dir as the slot's work dir.
        metadata = {**base.metadata, "job_dir": f" {chosen} "}
        expected_work_dir = chosen
    else:
        metadata = {**base.metadata, "reaction_dir": f" {reaction} "}
        expected_work_dir = reaction
    entry = replace(base, metadata=metadata)
    token = _reserve(tmp_path)

    assert worker._on_worker_process_started(
        tmp_path,
        entry,
        process=FakeManagedProcess(pid=123),
        admission_token=token,
    )

    slot = get_slot(tmp_path, token)
    assert slot is not None
    assert slot.state == "active"
    assert slot.queue_id == "queue-1"
    assert slot.app_name == "orca_auto_orca"
    assert slot.task_id == "task-1"
    assert slot.owner_pid == 123
    assert slot.work_dir == str(expected_work_dir.resolve())
    [record] = list_job_location_records(tmp_path)
    assert record.job_id == "task-1"
    assert record.status == "running"
    assert record.original_run_dir == str(reaction.resolve())


def test_rejected_attach_stops_child_then_marks_selected_generation_then_releases(
    tmp_path: Path,
    worker: OrcaQueueWorker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = enqueue_entry(tmp_path, _entry(tmp_path))
    process = FakeManagedProcess()
    events: list[str] = []

    def stop(proc: object) -> bool:
        # The child is stopped while its row is still the running generation.
        assert proc is process
        current = _row(tmp_path, "queue-1")
        assert current is not None and current.status is QueueStatus.RUNNING
        events.append("stop")
        return True

    real_release_slot = worker_mod.release_slot

    def release(root: str | Path, token: str) -> object:
        # The failed mark is durable before capacity is released.
        current = _row(tmp_path, "queue-1")
        assert current is not None and current.status is QueueStatus.FAILED
        events.append("release")
        return real_release_slot(root, token)

    # os.killpg is the only external effect here; everything else is real.
    monkeypatch.setattr(worker_mod, "terminate_process_group", stop)
    monkeypatch.setattr(worker_mod, "release_slot", release)
    monkeypatch.setattr(worker, "_start_background_process", lambda **_kwargs: process)

    # The slot this start was given no longer exists, so the attach is rejected
    # and the start handles that once: stop, mark failed, release.
    assert not worker._start_job(tmp_path, entry, admission_token="slot-reaped")

    assert events == ["stop", "release"]
    assert "queue-1" not in worker._running
    failed = _row(tmp_path, "queue-1")
    assert failed is not None
    assert failed.status is QueueStatus.FAILED
    assert failed.error == "admission_slot_missing"
    assert failed.task_id == "task-1"
    assert terminal_replay_marker_from_entry(failed) is not None
    assert list_job_location_records(tmp_path) == []


def test_running_record_failure_does_not_untrack_started_child(
    tmp_path: Path,
    worker: OrcaQueueWorker,
    stable_process_identity: ProcessIdentity,
) -> None:
    (tmp_path / "reaction").mkdir()
    token = _reserve(tmp_path)
    _corrupt_index(tmp_path)

    assert worker._on_worker_process_started(
        tmp_path,
        _entry(tmp_path),
        process=FakeManagedProcess(),
        admission_token=token,
    )

    slot = get_slot(tmp_path, token)
    assert slot is not None
    assert slot.state == "active"
    assert slot.queue_id == "queue-1"


@pytest.mark.parametrize(
    ("rc", "cancel_requested", "expected_status"),
    [
        (0, False, QueueStatus.COMPLETED),
        (17, False, QueueStatus.FAILED),
        (0, True, QueueStatus.CANCELLED),
    ],
)
def test_terminal_mark_result_carries_premark_snapshot_and_run_id(
    tmp_path: Path,
    rc: int,
    cancel_requested: bool,
    expected_status: QueueStatus,
) -> None:
    state = write_run_state(tmp_path / "reaction", status=RunStatus.RUNNING, job_id="task-1")
    enqueue_entry(tmp_path, _entry(tmp_path))
    if cancel_requested:
        assert cancel(tmp_path, "queue-1") is not None
    before = _row(tmp_path, "queue-1")
    assert before is not None

    result = replay.mark_terminal_queue_entry("queue-1", _job(tmp_path), rc=rc)

    # Cancellation takes precedence over the exit code; the result keeps the
    # pre-mark row snapshot and the run id read for that generation.
    assert result == replay.TerminalQueueMarkResult(
        True, expected_status.value, "task-1", before, tmp_path.resolve(), state["run_id"]
    )
    after = _row(tmp_path, "queue-1")
    assert after is not None
    assert after.status is expected_status
    assert after.task_id == "task-1"
    assert terminal_replay_marker_from_entry(after) is not None
    if expected_status is not QueueStatus.CANCELLED:
        assert after.metadata["run_id"] == state["run_id"]


@pytest.mark.parametrize("current_kind", ["missing", "pending", "replacement"])
def test_terminal_mark_does_not_touch_missing_nonrunning_or_new_generation(
    tmp_path: Path,
    current_kind: str,
) -> None:
    write_run_state(tmp_path / "reaction", status=RunStatus.RUNNING, job_id="task-1")
    if current_kind != "missing":
        enqueue_entry(
            tmp_path,
            _entry(
                tmp_path,
                status=QueueStatus.PENDING if current_kind == "pending" else QueueStatus.RUNNING,
                task_id="task-new" if current_kind == "replacement" else "task-1",
            ),
        )
    queue_file = tmp_path / QUEUE_FILE_NAME
    queue_bytes = queue_file.read_bytes() if queue_file.exists() else None
    current = _row(tmp_path, "queue-1")

    result = replay.mark_terminal_queue_entry("queue-1", _job(tmp_path), rc=0)

    assert not result.marked
    assert result.status is None
    assert result.expected_job_id == "task-1"
    assert result.current_entry == current
    assert (queue_file.read_bytes() if queue_file.exists() else None) == queue_bytes


def test_terminal_mark_reports_rejected_generation_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = write_run_state(tmp_path / "reaction", status=RunStatus.RUNNING, job_id="task-1")
    enqueue_entry(tmp_path, _entry(tmp_path))
    current = _row(tmp_path, "queue-1")
    # A fenced write that loses the race between the row read and the mark
    # cannot be produced in one process; the store answer is stubbed.
    monkeypatch.setattr(replay, "mark_completed", lambda *_args, **_kwargs: False)

    result = replay.mark_terminal_queue_entry("queue-1", _job(tmp_path), rc=0)

    assert result == replay.TerminalQueueMarkResult(
        False, None, "task-1", current, tmp_path.resolve(), state["run_id"]
    )
    assert _row(tmp_path, "queue-1") == current


@pytest.mark.parametrize("failure", ["terminate", "surviving", "recover", "mark"])
def test_cancel_failure_retains_slot_and_retry_owner(
    tmp_path: Path,
    worker: OrcaQueueWorker,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    enqueue_entry(tmp_path, _entry(tmp_path))
    token = _reserve(tmp_path, queue_id="queue-1")
    job = _job(tmp_path, admission_token=token)
    if failure == "surviving":
        job.process = FakeManagedProcess(poll_result=None)
    if failure == "recover":
        # The child's engine launch is still pending under this live owner, so
        # slot recovery refuses to run.
        assert prepare_slot_engine_process(tmp_path, token) is not None
    if failure == "mark":
        (tmp_path / QUEUE_FILE_NAME).write_text("{not a queue", encoding="utf-8")

    def terminate(_proc: object) -> bool:
        if failure == "terminate":
            raise OSError("stop failed")
        return True

    # os.killpg is the only external effect; everything else is real.
    monkeypatch.setattr(worker_mod, "terminate_process_group", terminate)

    assert not worker._cancel_running_job("queue-1", job)

    assert get_slot(tmp_path, token) is not None
    if failure != "mark":
        current = _row(tmp_path, "queue-1")
        assert current is not None
        assert current.status is QueueStatus.RUNNING
    if failure in {"recover", "mark"}:
        assert job.terminal_finalize_pending


def test_reconciliation_keeps_scoped_and_legacy_live_slot_protection(
    tmp_path: Path,
    worker: OrcaQueueWorker,
) -> None:
    rows = []
    for name in ("live", "legacy", "dead"):
        reaction = tmp_path / name
        reaction.mkdir()
        rows.append(
            make_queue_entry(
                queue_id=f"queue-{name}",
                task_id=f"task-{name}",
                reaction_dir=reaction,
                status=QueueStatus.RUNNING,
            )
        )
    save_entries_core(tmp_path, rows)
    _reserve(tmp_path, queue_id="queue-live", work_dir=tmp_path / "live")
    _reserve(tmp_path, queue_id="queue-legacy")

    worker._reconcile_worker_state()

    # A live slot scoped to its work dir and a legacy unscoped live slot both
    # protect their running rows; the row without a slot is recovered.
    assert {entry.queue_id: entry.status for entry in list_queue(tmp_path)} == {
        "queue-live": QueueStatus.RUNNING,
        "queue-legacy": QueueStatus.RUNNING,
        "queue-dead": QueueStatus.PENDING,
    }
