"""``orca_auto.orca.queue.worker``: completion and terminal finalization."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import BoundedSemaphore, Event
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from orca_auto.core.admission import (
    admission_dir,
    get_slot,
    list_slots,
    prepare_slot_engine_process,
    reserve_slot,
    set_slot_engine_process,
)
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_BLOCKED_KEY,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_PREPARING,
    queue_record_publication_lock_path,
    queue_record_sync_metadata,
    queue_record_sync_state,
)
from orca_auto.core.queue.store import save_entries as save_entries_core
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.statuses import STATUS_CANCELLED, STATUS_COMPLETED, STATUS_FAILED
from orca_auto.core.utils.lock import file_lock
from orca_auto.orca import execution as execution_mod
from orca_auto.orca import notifications as lifecycle_notifications
from orca_auto.orca.config import AppConfig
from orca_auto.orca.queue import job_records, publication_repair
from orca_auto.orca.queue import replay as replay_mod
from orca_auto.orca.queue import worker as queue_worker_mod
from orca_auto.orca.queue.adapter import (
    DuplicateEntryError,
    cancel,
    enqueue,
    list_queue,
    mark_failed,
)
from orca_auto.orca.queue.models import OrcaRunningJob, TerminalReplayWorkItem
from orca_auto.orca.queue.replay import TerminalQueueMarkResult
from orca_auto.orca.queue.terminal_replay import terminal_replay_marker_from_entry
from orca_auto.orca.queue.worker import OrcaQueueWorker
from orca_auto.orca.queue.worker_tracking import notify_terminal_job_from_state
from orca_auto.orca.state import finalize_state, new_state, save_state
from orca_auto.orca.state_reading import load_state
from orca_auto.orca.statuses import RunStatus
from tests.conftest import (
    RecordingChannel,
    claim_next_entry,
    enqueue_entry,
    make_queue_entry,
    write_run_state,
)
from tests.engine_artifact_helpers import bind_report_generation
from tests.queue_worker_helpers import (
    WORKER_LOGGER,
    ChildStarter,
    FakeChildren,
    admission_file_identity,
    awaited_send,
    current_orca_queue_metadata,
    held_run_lock,
    insert_pending_successor,
    job_record,
    queue_row,
    queue_statuses,
    reconcile_statuses,
    reserve_job_slot,
    run_terminal_replay,
    running_job,
    write_completed_run_state,
)


def replay_item(root: Path, queue_id: str, reaction_dir: Path, *, resolved: bool = True) -> Any:
    return TerminalReplayWorkItem(
        queue_root=root,
        queue_id=queue_id,
        reaction_dir=str(reaction_dir),
        reaction_key=str(reaction_dir.resolve()) if resolved else str(reaction_dir),
        task_id=f"task-{queue_id}",
        observed_status="failed",
        selected_inp="",
        error="",
    )


# ---------------------------------------------------------------------------
# Completion and terminal finalization
# ---------------------------------------------------------------------------


def test_check_completed_jobs_success(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_done"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    claim_next_entry(queue_root)
    write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exited=0), token, task_id=None
    )

    worker._check_completed_jobs()

    assert len(worker._running) == 0
    assert len(list_slots(admission_dir(queue_root))) == 0
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.COMPLETED}


def test_check_completed_jobs_leaves_a_deferred_child_pending(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    queue_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The child was refused RAM scratch before ORCA started, returned its
    # own row to the queue and exited non-zero. That exit code must not
    # fail the pending row, and the slot must become reusable.
    rxn = queue_root / "mol_deferred"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    claim_next_entry(queue_root)
    assert queue_worker_mod.requeue_running_entry(
        queue_root,
        entry.queue_id,
        admission_deferral_reason="engine scratch cannot guarantee RAM headroom",
    )
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exited=75), token, task_id=None
    )

    with caplog.at_level(logging.WARNING, logger=WORKER_LOGGER):
        worker._check_completed_jobs()

    assert len(worker._running) == 0
    assert len(list_slots(admission_dir(queue_root))) == 0
    [updated] = list_queue(queue_root)
    assert (updated.status, updated.error) == (QueueStatus.PENDING, "")
    assert any(
        "waits in the queue" in record.getMessage() and "RAM headroom" in record.getMessage()
        for record in caplog.records
    )
    assert not (rxn / "job_state.json").exists()


def test_check_completed_jobs_failure(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_fail"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exited=1), "slot_fail", task_id=None
    )

    worker._check_completed_jobs()

    assert len(worker._running) == 0
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.FAILED}


def test_check_completed_jobs_still_running(
    worker: OrcaQueueWorker, fake_children: FakeChildren
) -> None:
    worker._running["q_run"] = OrcaRunningJob(
        queue_root=worker.queue_root,
        queue_id="q_run",
        reaction_dir="/tmp/r",
        process=fake_children.spawn(),
        admission_token="slot_run",
    )
    worker._check_completed_jobs()
    assert len(worker._running) == 1


def test_completed_job_retries_when_engine_recovery_raises(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    sleeping_child: Callable[[], subprocess.Popen[bytes]],
    queue_root: Path,
) -> None:
    # The slot records an engine launch pending under a live owner, so the
    # engine identity cannot be recovered yet. The exited job must be retained
    # (row running, slot held) until recovery succeeds; here the owner dies.
    rxn = queue_root / "mol_recovery_retry"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-recovery")
    claim_next_entry(queue_root)
    owner = sleeping_child()
    token = reserve_job_slot(
        admission_dir(queue_root),
        worker.max_concurrent,
        entry,
        rxn,
        owner_pid=owner.pid,
        engine_process_state="idle",
        engine_launch_gated=True,
    )
    assert prepare_slot_engine_process(admission_dir(queue_root), token) is not None
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exited=1), token
    )

    worker._check_completed_jobs()

    assert entry.queue_id in worker._running
    assert len(list_slots(admission_dir(queue_root))) == 1
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.RUNNING}

    owner.kill()
    owner.wait()
    worker._check_completed_jobs()

    assert entry.queue_id not in worker._running
    assert len(list_slots(admission_dir(queue_root))) == 0
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.FAILED}


def test_failed_state_write_leaves_durable_replay_for_worker_restart(
    make_worker: Callable[..., OrcaQueueWorker], fake_children: FakeChildren, queue_root: Path
) -> None:
    worker = make_worker()
    rxn = queue_root / "mol_durable_restart"
    rxn.mkdir()
    old_state = new_state(rxn, rxn / "task-a.inp")
    old_state["job_id"] = "task-a"
    finalize_state(
        rxn,
        old_state,
        status=STATUS_COMPLETED,
        final_result={"status": STATUS_COMPLETED, "reason": "normal_termination"},
    )
    entry = enqueue(queue_root, str(rxn), force=True, task_id="task-b")
    claim_next_entry(queue_root)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=1), token)

    # Another ORCA instance holds the directory: the failed run state cannot be written.
    with held_run_lock(rxn), pytest.raises(RuntimeError, match="already running"):
        worker._finalize_completed_job(entry.queue_id, job, rc=1)

    assert len(list_slots(admission_dir(queue_root))) == 1
    [terminal] = list_queue(queue_root)
    assert terminal.status == QueueStatus.FAILED
    marker = terminal.metadata.get("orca_terminal_replay")
    assert isinstance(marker, dict)
    assert marker["task_id"] == "task-b"
    assert marker["observed_state"]["job_id"] == "task-a"

    restarted = make_worker()
    run_terminal_replay(restarted, terminal)

    written = load_state(rxn)
    assert written is not None
    assert (written["job_id"], written["status"]) == ("task-b", STATUS_FAILED)
    [replayed] = list_queue(queue_root)
    assert replayed.metadata.get("orca_terminal_replay") is None


def test_terminal_side_effect_failure_withholds_only_the_same_directory(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    queue_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = make_worker(start=child_starter)
    rxn = queue_root / "mol_terminal_replay_barrier"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-a")
    claim_next_entry(queue_root)
    token = reserve_job_slot(admission_dir(queue_root), 2, entry, rxn)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exited=1), token
    )
    unrelated = queue_root / "mol_unrelated"
    unrelated.mkdir()

    with held_run_lock(rxn):
        worker._check_completed_jobs()

        assert entry.queue_id in worker._running
        assert len(list_slots(admission_dir(queue_root))) == 1
        [pending_replay] = list_queue(queue_root)
        assert isinstance(pending_replay.metadata.get("orca_terminal_replay"), dict)
        with pytest.raises(DuplicateEntryError):
            enqueue(queue_root, str(rxn), force=True, task_id="task-b")
        # One of two slots is free, so what holds the successor back is the
        # replay barrier, not capacity. With nothing else pending the poll
        # is idle and leaves the admission file alone.
        successor = insert_pending_successor(queue_root, rxn, queue_id="q_forced_successor")
        before = admission_file_identity(admission_dir(queue_root))
        with caplog.at_level(logging.WARNING, logger=WORKER_LOGGER):
            assert worker._fill_slots() == "idle"
        assert admission_file_identity(admission_dir(queue_root)) == before
        assert any(str(rxn.resolve()) in record.getMessage() for record in caplog.records)
        assert child_starter.started == []

        # An unrelated job queued behind the withheld row is admitted.
        other = enqueue(
            queue_root,
            str(unrelated),
            task_id="task-unrelated",
            metadata=current_orca_queue_metadata(unrelated),
        )
        assert worker._fill_slots() == "processed"
        assert other.queue_id in worker._running
        assert successor.queue_id not in worker._running
        statuses = queue_statuses(queue_root)
        assert statuses[successor.queue_id] == QueueStatus.PENDING
        assert statuses[other.queue_id] == QueueStatus.RUNNING
        assert len(child_starter.started) == 1

    worker._check_completed_jobs()

    assert entry.queue_id not in worker._running
    assert queue_row(queue_root, entry.queue_id).metadata.get("orca_terminal_replay") is None
    written = load_state(rxn)
    assert written is not None
    assert (written["job_id"], written["status"]) == ("task-a", STATUS_FAILED)
    # The previous generation is published: its successor may start.
    assert worker._fill_slots() == "processed"
    assert successor.queue_id in worker._running
    assert len(child_starter.started) == 2


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("restart", [False, True])
def test_terminal_index_failure_releases_capacity_and_replays_after_recovery(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
    outcome: str,
    restart: bool,
) -> None:
    worker = make_worker(max_concurrent=1, start=child_starter)
    rxn = queue_root / "terminal_index_failure"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-terminal-index")
    claim_next_entry(queue_root)
    if outcome == "completed":
        write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    token = reserve_job_slot(admission_dir(queue_root), 1, entry, rxn)
    child = fake_children.spawn(exited=None if outcome == "cancelled" else int(outcome == "failed"))
    worker._running[entry.queue_id] = running_job(worker, entry, rxn, child, token)
    if outcome == "cancelled":
        cancel(queue_root, entry.queue_id)

    other_dir = queue_root / "terminal_index_unrelated"
    other = enqueue(
        queue_root,
        str(other_dir),
        task_id="task-unrelated",
        metadata=current_orca_queue_metadata(other_dir),
    )
    job_records.upsert_queued_job_record(worker.cfg, other)
    index_path = queue_root / "job_locations.json"
    index_before = index_path.read_bytes()
    index_path.write_text("{unreadable index", encoding="utf-8")

    if outcome == "cancelled":
        worker._check_cancel_requests()
    else:
        worker._check_completed_jobs()

    # Execution capacity is independent of the derived index. The durable
    # marker still owns this generation even after the reaped child is dropped.
    assert entry.queue_id not in worker._running
    assert get_slot(admission_dir(queue_root), token) is None
    terminal = queue_row(queue_root, entry.queue_id)
    assert terminal.status.value == outcome
    assert terminal_replay_marker_from_entry(terminal) is not None
    saved = load_state(rxn)
    assert saved is not None and saved["status"] == outcome
    run_id = saved["run_id"]
    assert recording_channel.sends == []

    if restart:
        worker = make_worker(max_concurrent=1, start=child_starter)
        worker._reconcile_worker_state()
    assert len(worker.replay_state.pending_replays) == 1
    with pytest.raises(DuplicateEntryError):
        enqueue(queue_root, str(rxn), force=True, task_id="task-successor")
    # Even a separately inserted successor cannot claim the freed slot.
    successor = insert_pending_successor(queue_root, rxn, queue_id="q_wait_for_publication")
    assert worker._fill_slots() == "processed"
    assert list(worker._running) == [other.queue_id]
    assert queue_statuses(queue_root)[successor.queue_id] == QueueStatus.PENDING
    assert [started.entry.queue_id for started in child_starter.started] == [other.queue_id]

    # Remove the deliberately injected competing row before recovery so the
    # durable terminal generation remains the unambiguous artifact owner.
    save_entries_core(
        queue_root, [row for row in list_queue(queue_root) if row.queue_id != successor.queue_id]
    )
    index_path.write_bytes(index_before)
    delivered = awaited_send(recording_channel)
    worker._reconcile_worker_state()
    assert delivered.wait(1)
    worker._reconcile_worker_state()
    record = job_record(queue_root, entry.task_id)
    assert record is not None and record["status"] == outcome
    assert queue_row(queue_root, entry.queue_id).metadata.get("orca_terminal_replay") is None
    assert worker.replay_state.pending_replays == {}
    saved = load_state(rxn)
    assert saved is not None and saved["run_id"] == run_id
    assert len(recording_channel.sends) == 1
    # A normal force submission is admitted by the generation fence again.
    assert enqueue(queue_root, str(rxn), force=True, task_id="task-successor")


@pytest.mark.parametrize("state_problem", ["missing", "unreadable", "running"])
def test_completed_child_retains_capacity_until_terminal_evidence_is_ready(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
    state_problem: str,
) -> None:
    rxn = queue_root / "terminal_evidence_missing"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-evidence")
    claim_next_entry(queue_root)
    if state_problem == "unreadable":
        (rxn / "job_state.json").write_text("{unreadable state", encoding="utf-8")
    elif state_problem == "running":
        write_run_state(rxn, status=RunStatus.RUNNING, job_id=entry.task_id)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=0), token)
    worker._running[entry.queue_id] = job

    worker._check_completed_jobs()
    assert worker._running[entry.queue_id] is job
    assert get_slot(admission_dir(queue_root), token) is not None
    assert job.pending_terminal_replay is not None
    assert not job.pending_terminal_replay.state_prepared
    assert worker.replay_state.pending_replays == {}
    assert terminal_replay_marker_from_entry(queue_row(queue_root, entry.queue_id))
    assert job_record(queue_root, entry.task_id) is None
    assert recording_channel.sends == []

    if state_problem == "running":
        state = load_state(rxn)
        assert state is not None
        finalize_state(
            rxn,
            state,
            status=STATUS_COMPLETED,
            final_result={"status": STATUS_COMPLETED, "reason": "normal_termination"},
        )
    else:
        write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    worker._check_completed_jobs()
    assert entry.queue_id not in worker._running
    assert get_slot(admission_dir(queue_root), token) is None
    assert queue_row(queue_root, entry.queue_id).metadata.get("orca_terminal_replay") is None
    assert job_record(queue_root, entry.task_id) is not None


@pytest.mark.parametrize("actual_status", [RunStatus.FAILED, RunStatus.CANCELLED])
def test_terminal_evidence_corrects_zero_exit_status_before_capacity_is_returned(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    queue_root: Path,
    actual_status: RunStatus,
) -> None:
    rxn = queue_root / "terminal_exit_disagreement"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-actual-outcome")
    claim_next_entry(queue_root)
    write_run_state(rxn, status=actual_status, job_id=entry.task_id)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=0), token)
    release = worker._release_admission_slot
    seen: list[tuple[str, str]] = []

    def observe_release(admission_token: str) -> object:
        row = queue_row(queue_root, entry.queue_id)
        seen.append((row.status.value, str(row.metadata.get("run_id"))))
        return release(admission_token)

    with patch.object(worker, "_release_admission_slot", side_effect=observe_release):
        worker._finalize_completed_job(entry.queue_id, job, rc=0)

    saved = load_state(rxn)
    assert saved is not None
    assert seen == [(actual_status.value, saved["run_id"])]
    record = job_record(queue_root, entry.task_id)
    assert record is not None and record["status"] == actual_status.value
    assert get_slot(admission_dir(queue_root), token) is None


def test_terminal_slot_release_failure_keeps_retry_owner_before_publication(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
) -> None:
    rxn = queue_root / "terminal_release_failure"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-release-failure")
    claim_next_entry(queue_root)
    write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=0), token)
    worker._running[entry.queue_id] = job

    with patch.object(worker, "_release_admission_slot", side_effect=OSError("slot write failed")):
        worker._check_completed_jobs()

    assert worker._running[entry.queue_id] is job
    assert job.terminal_finalize_pending
    assert job.pending_terminal_replay is not None
    assert job.pending_terminal_replay.state_prepared
    assert job.pending_terminal_replay.queue_id in worker.replay_state.pending_replays
    assert get_slot(admission_dir(queue_root), token) is not None
    assert terminal_replay_marker_from_entry(queue_row(queue_root, entry.queue_id))
    assert job_record(queue_root, entry.task_id) is None
    assert recording_channel.sends == []

    assert replay_mod.update_terminal(
        queue_root, entry.queue_id, STATUS_FAILED, expected_task_id=entry.task_id
    )
    delivered = awaited_send(recording_channel)
    worker._check_completed_jobs()
    assert delivered.wait(1)
    assert queue_row(queue_root, entry.queue_id).status == QueueStatus.COMPLETED
    assert entry.queue_id not in worker._running
    assert get_slot(admission_dir(queue_root), token) is None
    assert job.pending_terminal_replay is None
    assert not job.terminal_finalize_pending
    assert worker.replay_state.pending_replays == {}
    assert queue_row(queue_root, entry.queue_id).metadata.get("orca_terminal_replay") is None
    assert job_record(queue_root, entry.task_id) is not None
    assert len(recording_channel.sends) == 1


def test_pending_publication_rechecks_current_queue_outcome_on_retry(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "terminal_queue_correction"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-queue-correction")
    claim_next_entry(queue_root)
    write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=0), token)
    index_path = queue_root / "job_locations.json"
    index_path.write_text("{unreadable index", encoding="utf-8")
    worker._finalize_completed_job(entry.queue_id, job, rc=0)
    assert worker.replay_state.pending_replays
    # A competing terminal projection changed after the work item was prepared.
    # Retry must compare with the current row, not its cached observed status.
    assert replay_mod.update_terminal(
        queue_root, entry.queue_id, STATUS_FAILED, expected_task_id=entry.task_id
    )
    index_path.unlink()

    worker._reconcile_worker_state()

    assert queue_row(queue_root, entry.queue_id).status == QueueStatus.COMPLETED
    record = job_record(queue_root, entry.task_id)
    assert record is not None and record["status"] == STATUS_COMPLETED
    assert worker.replay_state.pending_replays == {}
    assert queue_row(queue_root, entry.queue_id).metadata.get("orca_terminal_replay") is None


def test_terminal_marker_clear_noop_retains_replay_without_execution_capacity(
    make_worker: Callable[..., OrcaQueueWorker],
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
) -> None:
    worker = make_worker()
    rxn = queue_root / "terminal_marker_failure"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-marker-failure")
    claim_next_entry(queue_root)
    write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exited=0), token
    )
    delivered = awaited_send(recording_channel)

    with patch.object(replay_mod, "_clear_terminal_replay_marker", return_value=False):
        worker._check_completed_jobs()
        assert delivered.wait(1)
        assert entry.queue_id not in worker._running
        assert get_slot(admission_dir(queue_root), token) is None
        assert len(worker.replay_state.pending_replays) == 1
        restarted = make_worker()
        restarted._reconcile_worker_state()
        # Both finish paths must verify a no-op instead of forgetting the marker.
        assert len(restarted.replay_state.pending_replays) == 1
        assert terminal_replay_marker_from_entry(queue_row(queue_root, entry.queue_id))
        assert job_record(queue_root, entry.task_id) is not None
        with pytest.raises(DuplicateEntryError):
            enqueue(queue_root, str(rxn), force=True, task_id="task-successor")

    restarted._reconcile_worker_state()
    assert restarted.replay_state.pending_replays == {}
    assert queue_row(queue_root, entry.queue_id).metadata.get("orca_terminal_replay") is None
    assert len(recording_channel.sends) == 1


def test_pending_replay_without_a_slot_does_not_pause_unrelated_jobs(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    # A replay found by reconciliation holds no slot and retries only every
    # minute; it used to pause the whole queue for as long as it lasted.
    worker = make_worker(max_concurrent=1, start=child_starter)
    withheld_dir = queue_root / "mol_replay_pending"
    unrelated = queue_root / "mol_replay_unrelated"
    withheld_row = enqueue(
        queue_root,
        str(withheld_dir),
        task_id="task-withheld",
        metadata=current_orca_queue_metadata(withheld_dir),
    )
    other = enqueue(
        queue_root,
        str(unrelated),
        task_id="task-unrelated",
        metadata=current_orca_queue_metadata(unrelated),
    )
    item = replay_item(queue_root, "q_closed_generation", withheld_dir)
    worker.replay_state.pending_replays[item.queue_id] = item

    assert worker._fill_slots() == "processed"

    assert list(worker._running) == [other.queue_id]
    assert queue_statuses(queue_root)[withheld_row.queue_id] == QueueStatus.PENDING


def test_unpublished_generation_without_a_directory_pauses_all_admission(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    queue_root: Path,
) -> None:
    worker = make_worker(start=child_starter)
    unrelated = queue_root / "mol_unknown_key_unrelated"
    other = enqueue(
        queue_root,
        str(unrelated),
        task_id="task-unrelated",
        metadata=current_orca_queue_metadata(unrelated),
    )
    job = OrcaRunningJob(
        queue_root=worker.queue_root,
        queue_id="q_unknown_directory",
        reaction_dir="",
        process=fake_children.spawn(),
        admission_token="slot_unknown_directory",
    )
    job.terminal_finalize_pending = True
    worker._running[job.queue_id] = job

    assert worker._fill_slots() == "blocked"

    assert child_starter.started == []
    assert list(worker._running) == [job.queue_id]
    [row] = list_queue(queue_root)
    assert (row.queue_id, row.status) == (other.queue_id, QueueStatus.PENDING)


@pytest.mark.parametrize("failure", ["busy", "index"])
def test_publication_repair_failure_withholds_only_its_row_and_later_recovers(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    queue_root: Path,
    failure: str,
) -> None:
    worker = make_worker(start=child_starter)
    unpublished = queue_root / "mol_repair_pending"
    row = enqueue_entry(
        queue_root,
        make_queue_entry(
            reaction_dir=unpublished,
            task_id="task-unpublished",
            priority=1,
            metadata={
                **current_orca_queue_metadata(unpublished),
                **queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_PREPARING, token="record_sync_test", owner_pid=0
                ),
            },
        ),
    )
    unrelated = queue_root / "mol_repair_failure_unrelated"
    other = enqueue(
        queue_root,
        str(unrelated),
        task_id="task-unrelated",
        metadata=current_orca_queue_metadata(unrelated),
    )
    job_records.upsert_queued_job_record(worker.cfg, other)
    index_path = queue_root / "job_locations.json"
    index_before = index_path.read_bytes()
    with ExitStack() as stack:
        if failure == "busy":
            lock_path = queue_record_publication_lock_path(queue_root, row.queue_id)
            lock_path.parent.mkdir(exist_ok=True)
            stack.enter_context(file_lock(lock_path, timeout_seconds=0.0))
        else:
            # The queue and snapshots remain readable while the shared index fails.
            index_path.write_text("{invalid index")
            stack.callback(index_path.write_bytes, index_before)

        assert worker._fill_slots() == "processed"
        assert list(worker._running) == [other.queue_id]
        assert [started.entry.queue_id for started in child_starter.started] == [other.queue_id]
        assert len(list_slots(admission_dir(queue_root))) == 1
        assert queue_statuses(queue_root) == {
            row.queue_id: QueueStatus.PENDING,
            other.queue_id: QueueStatus.RUNNING,
        }
        pending = queue_row(queue_root, row.queue_id)
        assert queue_record_sync_state(pending) != QUEUE_RECORD_SYNC_COMPLETE
        if failure == "index":
            blocker = pending.metadata[QUEUE_RECORD_SYNC_BLOCKED_KEY]
            assert "job_locations.json" in blocker["reason"]
            assert row.queue_id in blocker["next_action"]
        # A blocked row alone does not churn admission slots on each poll.
        before = admission_file_identity(admission_dir(queue_root))
        assert worker._fill_slots() == "idle"
        assert admission_file_identity(admission_dir(queue_root)) == before

    # Recovery automatically makes the original high-priority row eligible.
    assert worker._fill_slots() == "processed"
    repaired = queue_row(queue_root, row.queue_id)
    assert repaired.status == QueueStatus.RUNNING
    assert repaired.task_id == row.task_id
    assert queue_record_sync_state(repaired) == QUEUE_RECORD_SYNC_COMPLETE
    assert not repaired.metadata.get(QUEUE_RECORD_SYNC_BLOCKED_KEY)
    assert [started.entry.queue_id for started in child_starter.started] == [
        other.queue_id,
        row.queue_id,
    ]
    assert len(list_slots(admission_dir(queue_root))) == 2


def test_failed_publication_fence_and_diagnostic_write_withhold_only_unsafe_row(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    queue_root: Path,
) -> None:
    worker = make_worker(start=child_starter)
    unsafe_dir = queue_root / "unsafe"
    unsafe = enqueue(
        queue_root,
        str(unsafe_dir),
        priority=1,
        metadata=current_orca_queue_metadata(unsafe_dir),
    )
    other_dir = queue_root / "ready"
    other = enqueue(
        queue_root,
        str(other_dir),
        metadata=current_orca_queue_metadata(other_dir),
    )
    # The lease says COMPLETE, but its original directory has moved. Neither
    # terminal fencing nor blocker diagnostics can be persisted this pass.
    moved = queue_root / "original"
    unsafe_dir.rename(moved)
    unsafe_dir.mkdir()
    with (
        patch.object(publication_repair, "mark_failed", side_effect=OSError("fence write failed")),
        patch.object(
            publication_repair, "mutate_entries", side_effect=OSError("blocker write failed")
        ),
    ):
        assert worker._fill_slots() == "processed"
        assert list(worker._running) == [other.queue_id]
        unchanged = queue_row(queue_root, unsafe.queue_id)
        assert unchanged.status == QueueStatus.PENDING
        assert queue_record_sync_state(unchanged) == QUEUE_RECORD_SYNC_COMPLETE
        assert not unchanged.metadata.get(QUEUE_RECORD_SYNC_BLOCKED_KEY)
    # Restoring the exact directory identity clears the per-pass refusal.
    unsafe_dir.rmdir()
    moved.rename(unsafe_dir)
    assert worker._fill_slots() == "processed"
    assert unsafe.queue_id in worker._running


def test_unreadable_queue_still_blocks_admission_without_reserving_capacity(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    queue_root: Path,
) -> None:
    worker = make_worker(start=child_starter)
    job_dir = queue_root / "ready"
    entry = enqueue(
        queue_root,
        str(job_dir),
        metadata=current_orca_queue_metadata(job_dir),
    )
    queue_path = queue_root / "queue.json"
    original = queue_path.read_bytes()
    queue_path.write_text("{invalid queue")
    admission_path = admission_dir(queue_root) / "admission_slots.json"
    assert not admission_path.exists()
    assert worker._fill_slots() == "blocked"
    assert child_starter.started == []
    assert not admission_path.exists()
    assert queue_path.read_text() == "{invalid queue"
    queue_path.write_bytes(original)
    assert worker._fill_slots() == "processed"
    assert entry.queue_id in worker._running


def test_withheld_directory_is_matched_through_a_symlinked_spelling(
    worker: OrcaQueueWorker, queue_root: Path
) -> None:
    withheld_dir = queue_root / "mol_symlink_target"
    withheld_dir.mkdir()
    alias = queue_root / "mol_symlink_alias"
    alias.symlink_to(withheld_dir, target_is_directory=True)
    state = worker.replay_state
    state.admission_withheld_keys = frozenset({str(withheld_dir.resolve())})

    def row(reaction_dir: str) -> QueueEntry:
        return QueueEntry(
            queue_id="q_candidate",
            app_name="orca_auto_orca",
            task_id="task-candidate",
            task_kind="orca_run_inp",
            engine="orca",
            metadata={"reaction_dir": reaction_dir},
        )

    assert worker._entry_waits_for_terminal_replay(row(str(alias)))
    assert worker._entry_waits_for_terminal_replay(row(""))
    assert not worker._entry_waits_for_terminal_replay(row(str(queue_root / "other")))
    state.admission_withheld_keys = frozenset()
    assert not worker._entry_waits_for_terminal_replay(row(""))


def test_row_whose_directory_cannot_be_resolved_is_withheld_while_any_is(
    worker: OrcaQueueWorker, queue_root: Path
) -> None:
    state = worker.replay_state
    state.admission_withheld_keys = frozenset({str(queue_root / "mol_withheld")})
    # The row's directory is spelled through a user that does not exist:
    # it has no resolvable identity.
    candidate = QueueEntry(
        queue_id="q_unresolvable",
        app_name="orca_auto_orca",
        task_id="task-unresolvable",
        task_kind="orca_run_inp",
        engine="orca",
        metadata={"reaction_dir": "~no_such_user_orca_auto/mol_elsewhere"},
    )

    assert worker._entry_waits_for_terminal_replay(candidate)
    state.admission_withheld_keys = frozenset()
    assert not worker._entry_waits_for_terminal_replay(candidate)


def test_withheld_keys_follow_a_directory_retargeted_after_the_replay_item_was_built(
    worker: OrcaQueueWorker, queue_root: Path
) -> None:
    # The item froze its key when it was built. If the path is moved and the
    # old spelling becomes a symlink, a successor submitted through that
    # spelling resolves to the new location, which must be withheld as well.
    moved = queue_root / "proj_moved" / "job"
    moved.mkdir(parents=True)
    (queue_root / "proj").symlink_to(queue_root / "proj_moved", target_is_directory=True)
    item = replay_item(queue_root, "q_retargeted", queue_root / "proj" / "job", resolved=False)
    worker.replay_state.pending_replays[item.queue_id] = item

    assert worker._unresolved_terminal_reaction_keys() == frozenset(
        {str(queue_root / "proj" / "job"), str(moved.resolve())}
    )


def test_exited_job_awaiting_finalize_retry_withholds_its_directory_without_an_item(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    queue_root: Path,
) -> None:
    # Finalization failed before any replay item existed (for example engine
    # process recovery raised): only the retry flag and the job's own
    # directory identify what must be withheld.
    worker = make_worker(start=child_starter)
    retained_dir = queue_root / "mol_retry_only"
    unrelated = queue_root / "mol_retry_only_unrelated"
    same_dir_row = enqueue(
        queue_root,
        str(retained_dir),
        task_id="task-same-dir",
        metadata=current_orca_queue_metadata(retained_dir),
    )
    other = enqueue(
        queue_root,
        str(unrelated),
        task_id="task-unrelated",
        metadata=current_orca_queue_metadata(unrelated),
    )
    job = OrcaRunningJob(
        queue_root=worker.queue_root,
        queue_id="q_retry_only",
        reaction_dir=str(retained_dir),
        process=fake_children.spawn(exited=1),
        admission_token="slot_retry_only",
    )
    job.terminal_finalize_pending = True
    worker._running[job.queue_id] = job

    assert worker._fill_slots() == "processed"

    assert other.queue_id in worker._running
    assert same_dir_row.queue_id not in worker._running
    assert queue_statuses(queue_root)[same_dir_row.queue_id] == QueueStatus.PENDING


def test_withheld_directories_are_logged_when_the_set_changes_not_on_every_poll(
    worker: OrcaQueueWorker, queue_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    withheld_dir = queue_root / "mol_logged_once"
    withheld_dir.mkdir()
    item = replay_item(queue_root, "q_logged_once", withheld_dir)
    state = worker.replay_state
    state.pending_replays[item.queue_id] = item

    with caplog.at_level(logging.INFO, logger=WORKER_LOGGER):
        for _ in range(3):
            worker._fill_slots()
        state.pending_replays.clear()
        for _ in range(3):
            worker._fill_slots()

    records = [record for record in caplog.records if record.name == WORKER_LOGGER]
    assert [record.levelname for record in records] == ["WARNING", "INFO"]
    assert str(withheld_dir.resolve()) in records[0].getMessage()


def test_finalize_clears_active_engine_record_before_mark_and_release(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_active_engine_finalize"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-active-engine")
    claim_next_entry(queue_root)
    write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    token = reserve_job_slot(
        admission_dir(queue_root), worker.max_concurrent, entry, rxn, engine_process_state="idle"
    )
    prepare_slot_engine_process(admission_dir(queue_root), token)
    # The recorded engine group is gone (the child exited with it): recovery
    # must clear the active record before anything terminal is published.
    set_slot_engine_process(
        admission_dir(queue_root), token, pid=424242, pgid=424242, process_start_ticks=10101
    )
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=0), token)
    seen_at_mark: list[tuple[str | None, int]] = []
    real_mark = replay_mod.mark_terminal_queue_entry

    def mark(*args: Any, **kwargs: Any) -> TerminalQueueMarkResult:
        current = get_slot(admission_dir(queue_root), token)
        seen_at_mark.append(
            (
                current.engine_process_state if current else None,
                len(list_slots(admission_dir(queue_root))),
            )
        )
        return real_mark(*args, **kwargs)

    with patch.object(replay_mod, "mark_terminal_queue_entry", side_effect=mark):
        worker._finalize_completed_job(entry.queue_id, job, rc=0)

    # At mark time the engine record was already idle and the slot still held.
    assert seen_at_mark == [("idle", 1)]
    assert len(list_slots(admission_dir(queue_root))) == 0
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.COMPLETED}


def test_finalize_does_not_publish_without_persisted_marker_after_mark(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_mark_snapshot"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-b")
    claim_next_entry(queue_root)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=1), token)
    real_mark = replay_mod.mark_terminal_queue_entry

    def mark_then_lose_the_row(*args: Any, **kwargs: Any) -> TerminalQueueMarkResult:
        result = real_mark(*args, **kwargs)
        # Another actor removed the row right after the mark: no durable marker remains.
        save_entries_core(queue_root, [])
        return result

    with patch.object(replay_mod, "mark_terminal_queue_entry", side_effect=mark_then_lose_the_row):
        worker._finalize_completed_job(entry.queue_id, job, rc=1)

    # Nothing was published for the vanished generation, and the slot is free.
    assert not (rxn / "job_state.json").exists()
    assert job_record(queue_root, "task-b") is None
    assert list_queue(queue_root) == []
    assert len(list_slots(admission_dir(queue_root))) == 0


def test_stale_finalizer_does_not_resurrect_cleared_terminal_marker(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_stale_finalizer"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-stale-finalizer")
    running = claim_next_entry(queue_root)
    assert running is not None
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    assert mark_failed(queue_root, entry.queue_id, error="first_owner", expected_entry=running)
    assert replay_mod.update_queue_metadata(
        queue_root, entry.queue_id, {"orca_terminal_replay": None}
    )
    [closed] = list_queue(queue_root)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=1), token)

    worker._finalize_completed_job(entry.queue_id, job, rc=1)

    assert not (rxn / "job_state.json").exists()
    assert len(list_slots(admission_dir(queue_root))) == 0
    assert list_queue(queue_root) == [closed]
    assert closed.metadata.get("orca_terminal_replay") is None


def test_finalize_completed_job_recovers_once_and_releases_on_benign_mark_noop(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The row was moved or removed by another actor: nothing to mark, slot freed.
    moved = queue_root / "moved"
    token = reserve_slot(
        admission_dir(queue_root),
        worker.max_concurrent,
        work_dir=str(moved),
        queue_id="queue-moved",
        source="queue_worker",
        state="reserved",
    )
    assert token is not None
    job = OrcaRunningJob(
        queue_root=worker.queue_root,
        queue_id="queue-moved",
        reaction_dir=str(moved),
        process=fake_children.spawn(exited=1),
        admission_token=token,
        task_id="task-moved",
    )

    worker._finalize_completed_job(job.queue_id, job, rc=1)

    assert not moved.exists()
    assert job_record(queue_root, "task-moved") is None
    assert len(list_slots(admission_dir(queue_root))) == 0


def test_finalize_finished_job_clears_pending_launch_left_by_dead_child(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    sleeping_child: Callable[[], subprocess.Popen[bytes]],
    queue_root: Path,
) -> None:
    # The child died after fencing its launch but before publishing the
    # engine record. The gate wrapper never received its release byte, so
    # no engine ran: the finalizer must clear the pending record and
    # release the slot instead of retrying forever with reconcile paused.
    rxn = queue_root / "mol_pending_launch"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-pending-launch")
    claim_next_entry(queue_root)
    child = sleeping_child()
    token = reserve_job_slot(
        admission_dir(queue_root),
        worker.max_concurrent,
        entry,
        rxn,
        owner_pid=child.pid,
        engine_process_state="idle",
        engine_launch_gated=True,
    )
    assert prepare_slot_engine_process(admission_dir(queue_root), token) is not None
    child.kill()
    child.wait()
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=1), token)

    worker._finalize_completed_job(entry.queue_id, job, rc=1)

    record = job_record(queue_root, "task-pending-launch")
    assert record is not None and record["status"] == "failed"
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.FAILED}
    assert len(list_slots(admission_dir(queue_root))) == 0
    assert get_slot(admission_dir(queue_root), token) is None


def test_finalize_finished_job_marks_completed_and_releases_slot(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_completed"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)

    worker._finalize_completed_job(
        entry.queue_id, running_job(worker, entry, rxn, fake_children.spawn(exited=0), token), rc=0
    )

    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.COMPLETED}
    record = job_record(queue_root, entry.task_id)
    assert record is not None and record["status"] == "completed"
    assert len(list_slots(admission_dir(queue_root))) == 0


def test_finalize_finished_job_sends_parent_terminal_notification_when_unmarked(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
) -> None:
    delivered = awaited_send(recording_channel)
    rxn = queue_root / "mol_terminal_notify"
    rxn.mkdir()
    write_completed_run_state(rxn)
    entry = enqueue(queue_root, str(rxn), task_id="task_terminal_123")
    claim_next_entry(queue_root)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)

    worker._finalize_completed_job(
        entry.queue_id, running_job(worker, entry, rxn, fake_children.spawn(exited=0), token), rc=0
    )

    record = job_record(queue_root, "task_terminal_123")
    assert record is not None and record["status"] == "completed"
    assert delivered.wait(1)
    assert len(recording_channel.sends) == 1
    saved = load_state(rxn)
    assert saved is not None
    final_result = saved["final_result"]
    assert final_result is not None
    assert "finished_notification_claimed_at" in final_result
    assert "finished_notification_sent_at" not in final_result


@pytest.mark.parametrize("return_code", [0, 1])
def test_child_publishes_and_parent_releases_slot_while_terminal_sender_is_blocked(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    return_code: int,
) -> None:
    rxn = queue_root / "terminal_delivery"
    rxn.mkdir()
    selected = rxn / "calc.inp"
    selected.write_text("! HF STO-3G SP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    state = new_state(rxn, selected)
    state["job_id"] = entry.task_id
    generation = bind_report_generation(rxn, cast(dict[str, Any], state))
    selected = Path(state["selected_inp"])
    started, release = Event(), Event()
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(lifecycle_notifications, "_NOTIFICATION_SLOTS", slots)
    monkeypatch.setattr(execution_mod, "notification_channel", lambda _cfg: recording_channel)

    def on_send(message: object) -> None:
        if getattr(message, "title", "") in {"ORCA completed", "ORCA failed"}:
            started.set()
            assert release.wait(5), "test did not release terminal delivery"

    recording_channel.on_send = on_send

    class Runner:
        def run(self, inp: Path) -> SimpleNamespace:
            out = inp.with_suffix(".out")
            out.write_text("FINAL SINGLE POINT ENERGY -1.1\n****ORCA TERMINATED NORMALLY****\n")
            return SimpleNamespace(out_path=str(out), return_code=return_code)

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            child = pool.submit(
                execution_mod.run_with_state,
                cfg=worker.cfg,
                reaction_dir=rxn,
                selected_inp=selected,
                runner_cls=Runner,
                runner=Runner(),
                resumed=False,
                state=state,
            )
            assert child.result(timeout=2) == return_code
            assert not started.is_set(), "completion delivery must belong to the parent"
            reports = {path: path.read_bytes() for path in generation.iterdir() if path.is_file()}
            assert generation / "machine.json" in reports
            assert len(list_slots(admission_dir(queue_root))) == 1
            job = running_job(worker, entry, rxn, fake_children.spawn(exited=return_code), token)
            worker._running[entry.queue_id] = job
            finalizing = pool.submit(worker._check_completed_jobs)
            finalizing.result(timeout=2)
            assert started.wait(1)
            assert len(list_slots(admission_dir(queue_root))) == 0
            assert entry.queue_id not in worker._running
            [terminal] = list_queue(queue_root)
            expected = QueueStatus.COMPLETED if return_code == 0 else QueueStatus.FAILED
            assert terminal.status == expected
            assert terminal.metadata.get("orca_terminal_replay") is None
            saved = load_state(rxn)
            assert saved is not None and saved["final_result"] is not None
            assert saved["final_result"]["finished_notification_claimed_at"]
            assert "finished_notification_sent_at" not in saved["final_result"]
            run_terminal_replay(worker, terminal)
            run_terminal_replay(worker, terminal)
            assert len(recording_channel.sends) == 2  # one start, one terminal
            assert {path: path.read_bytes() for path in reports} == reports

            # A late sender must not stamp or republish a successor's state.
            successor = new_state(rxn, rxn / "next.inp")
            successor["job_id"] = "successor"
            save_state(rxn, successor)
            successor_bytes = (rxn / "job_state.json").read_bytes()
        finally:
            release.set()
    assert slots.acquire(timeout=2)  # sender's finally block has completed
    slots.release()
    assert (rxn / "job_state.json").read_bytes() == successor_bytes
    assert {path: path.read_bytes() for path in reports} == reports


def test_finalize_finished_job_releases_slot_when_terminal_notification_fails(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
) -> None:
    delivered = awaited_send(recording_channel, sent=False)
    rxn = queue_root / "mol_terminal_notify_failed"
    rxn.mkdir()
    write_completed_run_state(rxn)
    entry = enqueue(queue_root, str(rxn), task_id="task_terminal_123")
    claim_next_entry(queue_root)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=0), token)

    worker._finalize_completed_job(entry.queue_id, job, rc=0)

    record = job_record(queue_root, "task_terminal_123")
    assert record is not None and record["status"] == "completed"
    assert delivered.wait(1)
    assert len(recording_channel.sends) == 1
    [completed] = list_queue(queue_root)
    assert completed.status == QueueStatus.COMPLETED
    assert completed.metadata.get("orca_terminal_replay") is None
    assert len(list_slots(admission_dir(queue_root))) == 0
    assert entry.queue_id not in worker._running
    saved = load_state(rxn)
    assert saved is not None
    final_result = saved["final_result"]
    assert final_result is not None
    assert "finished_notification_sent_at" not in final_result


def test_terminal_notification_skips_when_state_already_marked(
    worker_cfg: AppConfig, recording_channel: RecordingChannel, queue_root: Path
) -> None:
    rxn = queue_root / "mol_terminal_already_marked"
    rxn.mkdir()
    write_completed_run_state(rxn)
    state = load_state(rxn)
    assert state is not None
    final_result = state["final_result"]
    assert final_result is not None
    final_result["finished_notification_sent_at"] = "2026-05-29T12:02:00+00:00"
    finalize_state(rxn, state, status="completed", final_result=final_result)

    assert notify_terminal_job_from_state(worker_cfg, str(rxn)) is False
    assert recording_channel.sends == []


def test_terminal_notification_rejects_previous_generation_state(
    worker_cfg: AppConfig, recording_channel: RecordingChannel, queue_root: Path
) -> None:
    rxn = queue_root / "mol_terminal_stale_generation"
    rxn.mkdir()
    write_completed_run_state(rxn)

    assert notify_terminal_job_from_state(worker_cfg, str(rxn), expected_job_id="task-b") is False
    assert recording_channel.sends == []


def test_finalize_finished_job_marks_failed_run(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_failed"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)

    worker._finalize_completed_job(
        entry.queue_id,
        running_job(worker, entry, rxn, fake_children.spawn(exited=2), token),
        rc=2,
    )

    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.FAILED}
    record = job_record(queue_root, entry.task_id)
    assert record is not None and record["status"] == "failed"


def test_finalize_finished_job_synthesizes_current_generation_failure_state(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
) -> None:
    delivered = awaited_send(recording_channel)
    rxn = queue_root / "mol_failed_before_current_state"
    rxn.mkdir()
    previous = new_state(rxn, rxn / "task-a.inp")
    previous["job_id"] = "task-a"
    previous_run_id = previous["run_id"]
    finalize_state(
        rxn,
        previous,
        status=STATUS_COMPLETED,
        final_result={
            "status": STATUS_COMPLETED,
            "reason": "normal_termination",
            "completed_at": "2026-07-10T00:00:00+00:00",
        },
    )
    entry = enqueue(queue_root, str(rxn), force=True, task_id="task-b")
    claim_next_entry(queue_root)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=1), token)

    worker._finalize_completed_job(entry.queue_id, job, rc=1)

    terminal = queue_row(queue_root, entry.queue_id)
    assert terminal.status == QueueStatus.FAILED
    assert terminal.metadata.get("run_id")
    assert terminal.metadata.get("run_id") != previous_run_id
    written = load_state(rxn)
    assert written is not None
    assert written["job_id"] == "task-b"
    assert written["run_id"] == terminal.metadata["run_id"]
    assert written["status"] == "failed"
    final_result = written["final_result"]
    assert final_result is not None
    assert (final_result["status"], final_result["reason"]) == ("failed", "exit_code=1")
    current_record = job_record(queue_root, "task-b")
    assert current_record is not None
    assert current_record["status"] == "failed"
    assert delivered.wait(1)
    assert len(recording_channel.sends) == 1

    run_terminal_replay(worker, terminal)
    run_terminal_replay(worker, terminal)

    assert len(recording_channel.sends) == 1
    assert reconcile_statuses(worker)[entry.queue_id] == "failed"


def test_finalize_finished_job_marks_cancelled_when_cancel_requested(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_requested_before_exit"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)

    worker._finalize_completed_job(
        entry.queue_id,
        running_job(worker, entry, rxn, fake_children.spawn(exited=143), token),
        rc=143,
    )

    [cancelled] = list_queue(queue_root)
    assert cancelled.status == QueueStatus.CANCELLED
    assert not cancelled.cancel_requested
    assert cancelled.metadata.get("run_id")
    written = load_state(rxn)
    assert written is not None
    assert written["job_id"] == entry.task_id
    assert written["run_id"] == cancelled.metadata["run_id"]
    assert written["status"] == STATUS_CANCELLED
    final_result = written["final_result"]
    assert final_result is not None
    assert final_result["status"] == STATUS_CANCELLED
    record = job_record(queue_root, entry.task_id)
    assert record is not None and record["status"] == "cancelled"
    assert len(list_slots(admission_dir(queue_root))) == 0
