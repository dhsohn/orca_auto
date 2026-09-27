"""``orca_auto.orca.queue.worker``: cancellation of queued and running jobs."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from orca_auto.core.admission import list_slots, prepare_slot_engine_process
from orca_auto.core.queue.store import save_entries as save_entries_core
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.statuses import STATUS_CANCELLED
from orca_auto.orca.config import AppConfig
from orca_auto.orca.queue import replay as replay_mod
from orca_auto.orca.queue import worker as queue_worker_mod
from orca_auto.orca.queue.adapter import cancel, enqueue, list_queue
from orca_auto.orca.queue.models import TerminalReplayWorkItem
from orca_auto.orca.queue.worker import OrcaQueueWorker
from orca_auto.orca.state_reading import load_state
from tests.conftest import RecordingChannel, claim_next_entry
from tests.queue_worker_helpers import (
    FakeChildren,
    awaited_send,
    held_run_lock,
    insert_pending_successor,
    job_record,
    queue_row,
    queue_statuses,
    reserve_job_slot,
    running_job,
)


def pending_launch_slot(root: Path, limit: int, entry: QueueEntry, reaction_dir: Path) -> str:
    """A slot whose engine launch is pending under this (live) process: recovery must refuse."""

    token = reserve_job_slot(
        root,
        limit,
        entry,
        reaction_dir,
        owner_pid=os.getpid(),
        engine_process_state="idle",
        engine_launch_gated=True,
    )
    assert prepare_slot_engine_process(root, token) is not None
    return token


@contextmanager
def read_only(directory: Path) -> Iterator[None]:
    """Refuse new files under ``directory`` (existing files stay readable)."""

    if os.geteuid() == 0:
        pytest.skip("a read-only directory does not refuse writes to root")
    directory.chmod(0o500)
    try:
        yield
    finally:
        directory.chmod(0o700)


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_check_cancel_requests(
    worker: OrcaQueueWorker,
    sleeping_child: Callable[[], subprocess.Popen[bytes]],
    queue_root: Path,
) -> None:
    rxn = queue_root / "mol_cancel"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    child = sleeping_child()
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, child, "slot_cancel", task_id=None
    )

    worker._check_cancel_requests()

    assert child.poll() == -signal.SIGTERM
    assert entry.queue_id not in worker._running
    [cancelled] = list_queue(queue_root)
    assert cancelled.status == QueueStatus.CANCELLED
    assert not cancelled.cancel_requested
    assert cancelled.metadata.get("orca_terminal_replay") is None
    written = load_state(rxn)
    assert written is not None
    assert written["status"] == STATUS_CANCELLED


def test_check_cancel_requests_retains_live_job_when_termination_fails(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_live"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    child = fake_children.spawn(stubborn=True)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, child, "slot_cancel_live", task_id=None
    )

    worker._check_cancel_requests()

    assert [signum for _pid, signum in fake_children.signals] == [signal.SIGTERM, signal.SIGKILL]
    assert child.poll_result is None
    assert entry.queue_id in worker._running
    [row] = list_queue(queue_root)
    assert (row.status, row.cancel_requested) == (QueueStatus.RUNNING, True)


def test_check_cancel_requests_ignores_replacement_generation(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_replacement"
    rxn.mkdir()
    selected = enqueue(queue_root, str(rxn), task_id="task-a")
    running = claim_next_entry(queue_root)
    assert running is not None
    replacement = replace(running, task_id="task-b", cancel_requested=True)
    save_entries_core(queue_root, [replacement])
    child = fake_children.spawn()
    worker._running[selected.queue_id] = running_job(
        worker, selected, rxn, child, "slot-a", task_id="task-a"
    )

    worker._check_cancel_requests()

    assert fake_children.signals == []
    assert child.poll_result is None
    assert selected.queue_id in worker._running
    [durable] = list_queue(queue_root)
    assert (durable.task_id, durable.status, durable.cancel_requested) == (
        "task-b",
        QueueStatus.RUNNING,
        True,
    )


def test_cancel_releases_prepared_execution_before_terminal_side_effects(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_order"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-cancel-order")
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    child = fake_children.spawn(exit_code=0)
    job = running_job(worker, entry, rxn, child, token)
    seen_at_finalize: list[tuple[int | None, QueueStatus, int]] = []
    real_side_effects = replay_mod._run_terminal_replay_side_effects

    def side_effects(cfg: AppConfig, item: TerminalReplayWorkItem) -> None:
        [row] = list_queue(queue_root)
        seen_at_finalize.append((child.poll_result, row.status, len(list_slots(queue_root))))
        real_side_effects(cfg, item)

    with patch.object(
        replay_mod, "_run_terminal_replay_side_effects", side_effect=side_effects
    ) as finalize_cancelled:
        assert worker._cancel_running_job(entry.queue_id, job) is True

    # By the time the terminal side effects ran, the child had been stopped,
    # the row was durably cancelled and execution capacity was already free.
    assert seen_at_finalize == [(0, QueueStatus.CANCELLED, 0)]
    assert len(list_slots(queue_root)) == 0
    item = finalize_cancelled.call_args.args[1]
    assert (item.queue_id, item.task_id, item.state_prepared) == (
        entry.queue_id,
        entry.task_id,
        True,
    )


def test_cancel_mark_failure_retains_queue_slot_and_skips_finalization(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The row's generation moved on under the job (another submission
    # replaced task-a with task-b): the durable cancel mark must refuse.
    rxn = queue_root / "mol_cancel_mark_failure"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-a")
    running = claim_next_entry(queue_root)
    assert running is not None
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    save_entries_core(queue_root, [replace(running, task_id="task-b", cancel_requested=True)])
    job = running_job(worker, entry, rxn, fake_children.spawn(exit_code=0), token)

    assert worker._cancel_running_job(entry.queue_id, job) is False

    assert len(list_slots(queue_root)) == 1
    [still_running] = list_queue(queue_root)
    assert (still_running.status, still_running.task_id) == (QueueStatus.RUNNING, "task-b")
    assert not (rxn / "job_state.json").exists()


def test_cancel_mark_false_completion_retry_keeps_running_entry_slot(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_mark_false_retry"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-cancel-mark-false-retry")
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=0), token
    )

    with (
        # Cancel marks from the worker; the completion retry marks through the
        # replay engine's terminal mark. Both refuse here.
        patch.object(queue_worker_mod, "mark_cancelled", return_value=False),
        patch.object(replay_mod, "mark_cancelled", return_value=False),
    ):
        worker._check_cancel_requests()
        assert entry.queue_id in worker._running
        assert len(list_slots(queue_root)) == 1

        worker._check_completed_jobs()

    assert entry.queue_id in worker._running
    assert len(list_slots(queue_root)) == 1
    [still_running] = list_queue(queue_root)
    assert (still_running.status, still_running.cancel_requested) == (QueueStatus.RUNNING, True)


def test_cancel_mark_false_releases_after_concurrent_terminal_transition(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_mark_false_terminal_race"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-cancel-terminal-race")
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exited=0), token
    )
    real_mark_cancelled = replay_mod.mark_cancelled

    def terminalize_then_report_false(*args: Any, **kwargs: Any) -> bool:
        assert real_mark_cancelled(*args, **kwargs)
        return False

    # The completion path marks through the replay engine's terminal mark;
    # here the row turns terminal but the caller is told nothing changed.
    with patch.object(replay_mod, "mark_cancelled", side_effect=terminalize_then_report_false):
        worker._check_completed_jobs()

    written = load_state(rxn)
    assert written is not None
    assert (written["job_id"], written["status"]) == (entry.task_id, STATUS_CANCELLED)
    assert entry.queue_id not in worker._running
    assert len(list_slots(queue_root)) == 0
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.CANCELLED}


def test_cancel_mark_exception_isolated_and_retried_by_completion(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_mark_exception_retry"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-cancel-mark-exception-retry")
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=0), token
    )

    # The queue file cannot be rewritten while the cancel is processed.
    with read_only(queue_root):
        worker._check_cancel_requests()
        assert entry.queue_id in worker._running
        assert len(list_slots(queue_root)) == 1
        [still_running] = list_queue(queue_root)
        assert still_running.status == QueueStatus.RUNNING

    worker._check_completed_jobs()

    assert entry.queue_id not in worker._running
    assert len(list_slots(queue_root)) == 0
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.CANCELLED}
    written = load_state(rxn)
    assert written is not None
    assert written["status"] == STATUS_CANCELLED


def test_cancel_state_failure_retains_slot_after_terminal_mark(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
) -> None:
    delivered = awaited_send(recording_channel)
    rxn = queue_root / "mol_cancel_state_failure"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-cancel-state-failure")
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exit_code=0), token)
    worker._running[entry.queue_id] = job

    # The cancelled run state cannot be written while another instance owns the directory.
    with held_run_lock(rxn):
        assert worker._cancel_running_job(entry.queue_id, job) is False
        assert len(list_slots(queue_root)) == 1
        assert job_record(queue_root, entry.task_id) is None
        assert recording_channel.sends == []
        [cancelled_entry] = list_queue(queue_root)
        assert cancelled_entry.status == QueueStatus.CANCELLED
        assert job.pending_terminal_replay is not None

    worker._check_completed_jobs()

    assert len(list_slots(queue_root)) == 0
    assert entry.queue_id not in worker._running
    written = load_state(rxn)
    assert written is not None
    assert (written["job_id"], written["status"]) == (entry.task_id, STATUS_CANCELLED)
    record = job_record(queue_root, entry.task_id)
    assert record is not None and record["status"] == "cancelled"
    assert delivered.wait(1)
    assert len(recording_channel.sends) == 1


def test_cancel_side_effect_failure_withholds_its_directory_until_strict_replay(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_replay_barrier"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-cancel-a")
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(queue_root, 2, entry, rxn)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=0), token
    )

    with held_run_lock(rxn):
        worker._check_cancel_requests()

        assert entry.queue_id in worker._running
        assert len(list_slots(queue_root)) == 1
        [pending_replay] = list_queue(queue_root)
        assert pending_replay.status == QueueStatus.CANCELLED
        assert isinstance(pending_replay.metadata.get("orca_terminal_replay"), dict)
        successor = insert_pending_successor(queue_root, rxn, queue_id="q_cancel_successor")
        assert worker._unresolved_terminal_reaction_keys() == frozenset({str(rxn.resolve())})
        assert worker._fill_slots() == "idle"
        assert successor.queue_id not in worker._running

    worker._check_completed_jobs()

    assert entry.queue_id not in worker._running
    assert len(list_slots(queue_root)) == 0
    assert queue_row(queue_root, entry.queue_id).metadata.get("orca_terminal_replay") is None
    record = job_record(queue_root, "task-cancel-a")
    assert record is not None and record["status"] == "cancelled"
    assert worker._unresolved_terminal_reaction_keys() == frozenset()


def test_cancel_recovery_failure_retains_queue_slot_and_skips_mark(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_recovery_failure"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-cancel-recovery-failure")
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    # The engine launch is still pending under a live owner: recovery must refuse.
    token = pending_launch_slot(queue_root, worker.max_concurrent, entry, rxn)
    child = fake_children.spawn(exit_code=0)
    job = running_job(worker, entry, rxn, child, token)

    assert worker._cancel_running_job(entry.queue_id, job) is False

    assert child.poll_result == 0
    assert len(list_slots(queue_root)) == 1
    [still_running] = list_queue(queue_root)
    assert (still_running.status, still_running.cancel_requested) == (QueueStatus.RUNNING, True)
    assert not (rxn / "job_state.json").exists()
