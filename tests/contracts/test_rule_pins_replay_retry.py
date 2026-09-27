"""Rule pins: which terminal rows the worker's recovery pass replays, and when it retries.

Replay needs positive evidence that a row turned terminal while this worker was
watching: an active status seen by a previous pass, a durable replay marker or
a retained work item. The three pins drive a real ``OrcaQueueWorker`` through
recovery passes (``_reconcile_worker_state_now``) over rows written as another
writer leaves them, without a replay marker, and assert only durable outcomes:
``job_state.json``, ``job_locations.json``, the queue rows and the
notifications sent.

* An ambiguous generation owner is retried on every later pass, and replayed
  once state identity names it.
* Failed replay side effects are retried on every later pass until they succeed.
* A terminal row first seen at startup, or first seen already terminal after
  startup, is closed history and is never replayed.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

from orca_auto.core.queue.persistence import save_entries
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.job_locations import list_job_location_records
from orca_auto.orca.queue.adapter import list_queue
from orca_auto.orca.queue.worker import OrcaQueueWorker
from orca_auto.orca.state_reading import load_state, state_path
from orca_auto.orca.statuses import RunStatus
from tests.conftest import RecordingChannel, make_app_cfg, make_queue_entry, write_run_state


def _worker(root: Path) -> OrcaQueueWorker:
    return OrcaQueueWorker(make_app_cfg(root), str(root / "orca_auto.yaml"))


def _recovery_pass(worker: OrcaQueueWorker) -> None:
    worker._reconcile_worker_state_now()
    for thread in threading.enumerate():
        if thread.name.endswith("-notification"):
            thread.join(timeout=5)


def _row(job_dir: Path, queue_id: str, status: QueueStatus) -> QueueEntry:
    return make_queue_entry(
        queue_id=queue_id, task_id=f"task-{queue_id}", reaction_dir=job_dir, status=status
    )


def _terminal(entry: QueueEntry, status: QueueStatus) -> QueueEntry:
    return replace(entry, status=status, cancel_requested=status is QueueStatus.CANCELLED)


def test_ambiguous_generation_owner_is_retried_on_later_passes(
    tmp_path: Path, recording_channel: RecordingChannel
) -> None:
    root = tmp_path / "runs"
    job_dir = root / "job"
    job_dir.mkdir(parents=True)
    rows = [_row(job_dir, "a", QueueStatus.PENDING), _row(job_dir, "b", QueueStatus.PENDING)]
    save_entries(root, rows)
    worker = _worker(root)
    _recovery_pass(worker)

    # Both generations of one directory turn terminal and no state names either.
    save_entries(root, [_terminal(row, QueueStatus.CANCELLED) for row in rows])
    queue_bytes = (root / "queue.json").read_bytes()
    _recovery_pass(worker)
    _recovery_pass(worker)

    assert not state_path(job_dir).exists()
    assert list_job_location_records(root) == []
    assert recording_channel.sends == []
    assert (root / "queue.json").read_bytes() == queue_bytes

    write_run_state(job_dir, status=RunStatus.RUNNING, job_id="task-b")
    _recovery_pass(worker)

    written = load_state(job_dir)
    assert written is not None
    assert (written["job_id"], written["status"]) == ("task-b", "cancelled")
    [record] = list_job_location_records(root)
    assert (record.job_id, record.status) == ("task-b", "cancelled")
    assert len(recording_channel.sends) == 1
    replayed = {row.queue_id: row for row in list_queue(root)}
    assert replayed["b"].metadata["run_id"] == written["run_id"]
    assert "run_id" not in replayed["a"].metadata


def test_failed_replay_side_effects_are_retried_on_later_passes(
    tmp_path: Path, recording_channel: RecordingChannel
) -> None:
    root = tmp_path / "runs"
    job_dir = root / "job"
    job_dir.mkdir(parents=True)
    row = _row(job_dir, "a", QueueStatus.PENDING)
    save_entries(root, [row])
    worker = _worker(root)
    _recovery_pass(worker)

    # The row turns completed before its child published a terminal state.
    save_entries(root, [_terminal(row, QueueStatus.COMPLETED)])
    _recovery_pass(worker)
    _recovery_pass(worker)

    assert list_job_location_records(root) == []
    assert recording_channel.sends == []

    write_run_state(job_dir, status=RunStatus.COMPLETED, job_id="task-a")
    _recovery_pass(worker)
    _recovery_pass(worker)

    [record] = list_job_location_records(root)
    assert (record.job_id, record.status) == ("task-a", "completed")
    assert len(recording_channel.sends) == 1


def test_terminal_row_first_seen_terminal_is_never_replayed(
    tmp_path: Path, recording_channel: RecordingChannel
) -> None:
    root = tmp_path / "runs"
    at_startup = root / "at_startup"
    after_startup = root / "after_startup"
    for job_dir in (at_startup, after_startup):
        write_run_state(job_dir, status=RunStatus.COMPLETED, job_id=f"task-{job_dir.name}")
    first = _row(at_startup, "at_startup", QueueStatus.COMPLETED)
    save_entries(root, [first])
    worker = _worker(root)
    _recovery_pass(worker)

    save_entries(root, [first, _row(after_startup, "after_startup", QueueStatus.COMPLETED)])
    queue_bytes = (root / "queue.json").read_bytes()
    states = {job_dir: state_path(job_dir).read_bytes() for job_dir in (at_startup, after_startup)}
    _recovery_pass(worker)
    _recovery_pass(worker)

    assert (root / "queue.json").read_bytes() == queue_bytes
    assert {job_dir: state_path(job_dir).read_bytes() for job_dir in states} == states
    assert list_job_location_records(root) == []
    assert recording_channel.sends == []
