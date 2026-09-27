"""``orca_auto.orca.queue.worker``: shutdown of running children."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from orca_auto.core.admission import admission_dir, get_slot, list_slots
from orca_auto.core.queue.persistence import save_entries as save_entries_core
from orca_auto.core.queue.processes import ManagedProcess, terminate_process_group
from orca_auto.core.queue.types import QueueStatus
from orca_auto.orca.queue import worker as queue_worker_mod
from orca_auto.orca.queue.adapter import cancel, enqueue, list_queue, mark_failed
from orca_auto.orca.queue.terminal_replay import terminal_replay_marker_from_entry
from orca_auto.orca.queue.worker import OrcaQueueWorker
from orca_auto.orca.state import new_state, save_state
from orca_auto.orca.state_reading import load_state
from orca_auto.orca.types import RunFinalResult
from tests.conftest import claim_next_entry
from tests.queue_worker_helpers import (
    WORKER_LOGGER,
    FakeChildren,
    queue_row,
    queue_statuses,
    reserve_job_slot,
    running_job,
)


def save_child_state(
    reaction_dir: Path, job_id: str, status: str, final_result: RunFinalResult | None = None
) -> None:
    """The ``job_state.json`` a child left behind, as it would have written it."""

    state = new_state(reaction_dir, reaction_dir / "job.inp")
    state["job_id"] = job_id
    state["status"] = status
    if final_result is not None:
        state["final_result"] = final_result
    save_state(reaction_dir, state)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def test_shutdown_asks_every_child_to_stop_before_escalating_on_any(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The first child ignores SIGTERM for its whole graceful period. The
    # second must already have been asked to stop before the first is
    # SIGKILLed, so the graceful periods overlap instead of adding up.
    rxn_slow = queue_root / "mol_shut_slow"
    rxn_slow.mkdir()
    rxn_fast = queue_root / "mol_shut_fast"
    rxn_fast.mkdir()
    slow_entry = enqueue(queue_root, str(rxn_slow))
    fast_entry = enqueue(queue_root, str(rxn_fast))
    claim_next_entry(queue_root)
    claim_next_entry(queue_root)
    slow_child = fake_children.spawn(ignores_sigterm=True)
    fast_child = fake_children.spawn()
    worker._running[slow_entry.queue_id] = running_job(
        worker, slow_entry, rxn_slow, slow_child, "slot_slow", task_id=None
    )
    worker._running[fast_entry.queue_id] = running_job(
        worker, fast_entry, rxn_fast, fast_child, "slot_fast", task_id=None
    )

    worker._shutdown_all()

    signals = fake_children.signals
    # Both children are asked to stop up front; the slow one is asked again
    # in its own turn and then escalated. The fast child's request precedes
    # that escalation, so it stopped during the slow child's graceful period.
    assert signals[:2] == [(slow_child.pid, signal.SIGTERM), (fast_child.pid, signal.SIGTERM)]
    assert signals.index((fast_child.pid, signal.SIGTERM)) < signals.index(
        (slow_child.pid, signal.SIGKILL)
    )
    assert signals[-1] == (slow_child.pid, signal.SIGKILL)
    assert slow_child.poll_result == -signal.SIGKILL
    assert fake_children.stopped(fast_child)
    assert worker._running == {}
    statuses = queue_statuses(queue_root)
    assert statuses[slow_entry.queue_id] == QueueStatus.PENDING
    assert statuses[fast_entry.queue_id] == QueueStatus.PENDING


def test_start_warns_when_concurrency_exceeds_host_cores(
    make_worker: Callable[..., OrcaQueueWorker],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = make_worker(max_concurrent=2)
    cores_per_task = int(worker.cfg.resources.max_cores_per_task)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(cores_per_task)))

    with caplog.at_level(logging.WARNING, logger=WORKER_LOGGER):
        worker._before_run()

    [record] = [r for r in caplog.records if "oversubscribe" in r.getMessage()]
    message = record.getMessage()
    assert (
        f"max_concurrent=2 x max_cores_per_task={cores_per_task} requests {2 * cores_per_task} cores"
        in message
    )
    assert f"this worker can use {cores_per_task}" in message


def test_start_stays_quiet_when_concurrency_fits_host_cores(
    make_worker: Callable[..., OrcaQueueWorker],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = make_worker(max_concurrent=2)
    cores_per_task = int(worker.cfg.resources.max_cores_per_task)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(2 * cores_per_task)))

    with caplog.at_level(logging.WARNING, logger=WORKER_LOGGER):
        worker._before_run()

    assert not [r for r in caplog.records if "oversubscribe" in r.getMessage()]


def test_shutdown_all_empty(worker: OrcaQueueWorker) -> None:
    worker._shutdown_all()
    assert len(worker._running) == 0


def test_shutdown_all_with_running(
    worker: OrcaQueueWorker,
    sleeping_child: Callable[[], subprocess.Popen[bytes]],
    queue_root: Path,
) -> None:
    rxn = queue_root / "mol_shut"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    child = sleeping_child()
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, child, "slot_shutdown", task_id=None
    )

    worker._shutdown_all()

    assert child.poll() == -signal.SIGTERM
    assert len(worker._running) == 0
    [row] = list_queue(queue_root)
    assert (row.status, row.started_at) == (QueueStatus.PENDING, "")


def test_shutdown_does_not_requeue_a_replacement_generation(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The shutdown requeue is fenced to the generation the job was started
    # for: a row that another submission has since replaced stays untouched.
    rxn = queue_root / "mol_shut_replaced"
    rxn.mkdir()
    selected = enqueue(queue_root, str(rxn), task_id="task-a")
    running = claim_next_entry(queue_root)
    assert running is not None
    save_entries_core(queue_root, [replace(running, task_id="task-b")])
    child = fake_children.spawn()
    worker._running[selected.queue_id] = running_job(
        worker, selected, rxn, child, "slot-a", task_id="task-a"
    )

    worker._shutdown_all()

    assert fake_children.stopped(child)
    assert len(worker._running) == 0
    [durable] = list_queue(queue_root)
    assert (durable.task_id, durable.status) == ("task-b", QueueStatus.RUNNING)


def test_shutdown_finalizes_cancel_requested_job(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # A cancel pending when the worker shuts down must be finalized (terminal +
    # run state written), not requeued for resume: the shutdown path routes it
    # through the same cancel finalization as the proactive loop.
    rxn = queue_root / "mol_shut_cancel"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    save_child_state(rxn, entry.task_id, "running")
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=0), "slot_shut_cancel", task_id=None
    )

    worker._shutdown_all()

    assert entry.queue_id not in worker._running
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.CANCELLED}
    written = load_state(rxn)
    assert written is not None
    assert written["final_result"] is not None
    assert written["final_result"]["status"] == "cancelled"


def test_shutdown_finalizes_a_child_that_finished_instead_of_requeueing(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The child was still alive at the last poll but had already finished
    # ORCA and was writing its state/report; it exits 0 during the grace
    # window. Requeueing it would re-run (force rows) or unbind (plain rows)
    # a completed generation, so it takes the normal completion path.
    rxn = queue_root / "mol_shut_done"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    save_child_state(
        rxn,
        entry.task_id,
        "completed",
        {"status": "completed", "reason": "normal_termination", "analyzer_status": "completed"},
    )
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=0), "slot_shut_done", task_id=None
    )

    worker._shutdown_all()

    assert entry.queue_id not in worker._running
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.COMPLETED}


def test_shutdown_finalizes_a_child_that_finished_failed(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # A failed run also reached its own conclusion (state, report and
    # notification written) and exits 1; requeueing it would re-run a
    # generation the user was already told failed.
    rxn = queue_root / "mol_shut_failed"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    save_child_state(
        rxn,
        entry.task_id,
        "failed",
        {"status": "failed", "reason": "retry_limit_reached", "analyzer_status": "incomplete"},
    )
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=1), "slot_shut_failed", task_id=None
    )

    worker._shutdown_all()

    assert entry.queue_id not in worker._running
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.FAILED}


def test_shutdown_leaves_a_self_requeued_child_pending(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The common graceful-stop path: the child was stopped mid-run, requeued
    # its own row and exited 0. The completion path must not touch the
    # pending row and must still release the slot.
    rxn = queue_root / "mol_shut_self"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)

    def requeue_own_row() -> None:
        queue_worker_mod.requeue_running_entry(queue_root, entry.queue_id)

    worker._running[entry.queue_id] = running_job(
        worker,
        entry,
        rxn,
        fake_children.spawn(exit_code=0, on_stop=requeue_own_row),
        token,
    )

    worker._shutdown_all()

    assert entry.queue_id not in worker._running
    assert len(list_slots(admission_dir(queue_root))) == 0
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.PENDING}


def test_shutdown_continues_with_the_next_job_when_finalizing_one_fails(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn_done = queue_root / "mol_shut_done_raise"
    rxn_done.mkdir()
    rxn_live = queue_root / "mol_shut_live"
    rxn_live.mkdir()
    done_entry = enqueue(queue_root, str(rxn_done))
    live_entry = enqueue(queue_root, str(rxn_live))
    claim_next_entry(queue_root)
    claim_next_entry(queue_root)
    # The finished child left a terminal run state, so it is eligible for
    # the completion path; publishing it then fails on an unreadable job index.
    save_child_state(
        rxn_done,
        done_entry.task_id,
        "completed",
        {"status": "completed", "reason": "normal_termination", "analyzer_status": "completed"},
    )
    done_token = reserve_job_slot(
        admission_dir(queue_root), worker.max_concurrent, done_entry, rxn_done
    )
    (queue_root / "job_locations.json").mkdir()
    done_child = fake_children.spawn(exit_code=0)
    live_child = fake_children.spawn()
    worker._running[done_entry.queue_id] = running_job(
        worker, done_entry, rxn_done, done_child, done_token, task_id=None
    )
    worker._running[live_entry.queue_id] = running_job(
        worker, live_entry, rxn_live, live_child, "slot_live", task_id=None
    )

    worker._shutdown_all()

    assert fake_children.stopped(done_child) and fake_children.stopped(live_child)
    assert len(worker._running) == 0
    statuses = queue_statuses(queue_root)
    # The finished job keeps its durable publication marker for restart,
    # but releases execution capacity; the live one is requeued for resume.
    assert statuses[done_entry.queue_id] == QueueStatus.COMPLETED
    assert terminal_replay_marker_from_entry(queue_row(queue_root, done_entry.queue_id))
    assert get_slot(admission_dir(queue_root), done_token) is None
    assert statuses[live_entry.queue_id] == QueueStatus.PENDING


def test_shutdown_continues_with_the_next_job_when_terminating_one_fails(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # An exception before the completion path (here from termination
    # itself) must not leave the remaining children running unsupervised.
    rxn_broken = queue_root / "mol_shut_term_raise"
    rxn_broken.mkdir()
    rxn_live = queue_root / "mol_shut_live_2"
    rxn_live.mkdir()
    broken_entry = enqueue(queue_root, str(rxn_broken))
    live_entry = enqueue(queue_root, str(rxn_live))
    claim_next_entry(queue_root)
    claim_next_entry(queue_root)
    broken_child = fake_children.spawn()
    live_child = fake_children.spawn()
    worker._running[broken_entry.queue_id] = running_job(
        worker, broken_entry, rxn_broken, broken_child, "slot_term_raise", task_id=None
    )
    worker._running[live_entry.queue_id] = running_job(
        worker, live_entry, rxn_live, live_child, "slot_live_2", task_id=None
    )

    def terminate(process: ManagedProcess) -> bool:
        if process is broken_child:
            raise RuntimeError("simulated termination failure")
        return terminate_process_group(process)

    with patch.object(queue_worker_mod, "terminate_process_group", side_effect=terminate):
        worker._shutdown_all()

    assert len(worker._running) == 0
    # The core last resort stopped the child whose engine-level shutdown
    # raised, and the next job was still shut down normally.
    assert fake_children.stopped(broken_child) and fake_children.stopped(live_child)
    statuses = queue_statuses(queue_root)
    assert statuses[broken_entry.queue_id] == QueueStatus.RUNNING
    assert statuses[live_entry.queue_id] == QueueStatus.PENDING


def test_shutdown_requeues_a_child_that_died_handling_the_stop(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The child caught the stop but its own requeue write raised, so it
    # exited 1 with the row still running and a non-terminal run state.
    # That is an interrupted calculation, not a failed one.
    rxn = queue_root / "mol_shut_interrupted"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    save_child_state(rxn, entry.task_id, "running")
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=1), "slot_shut_interrupted", task_id=None
    )

    worker._shutdown_all()

    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.PENDING}
    written = load_state(rxn)
    assert written is not None
    assert written["status"] == "running"


def test_shutdown_tolerates_a_failed_cancel_read_and_still_stops_the_child(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The queue read that precedes termination raised; the child must still
    # be stopped and requeued through the ordinary path (the store-level
    # requeue honors a pending cancel on its own), and the next job is
    # still shut down.
    rxn_broken = queue_root / "mol_shut_pre_raise"
    rxn_broken.mkdir()
    rxn_live = queue_root / "mol_shut_live_3"
    rxn_live.mkdir()
    broken_entry = enqueue(queue_root, str(rxn_broken))
    live_entry = enqueue(queue_root, str(rxn_live))
    claim_next_entry(queue_root)
    claim_next_entry(queue_root)
    broken_child = fake_children.spawn()
    live_child = fake_children.spawn()
    worker._running[broken_entry.queue_id] = running_job(
        worker, broken_entry, rxn_broken, broken_child, "slot_pre_raise", task_id=None
    )
    worker._running[live_entry.queue_id] = running_job(
        worker, live_entry, rxn_live, live_child, "slot_live_3", task_id=None
    )
    real_get_cancel_requested = queue_worker_mod.get_cancel_requested

    def get_cancel_requested(queue_root_arg: Path, queue_id: str, **kwargs: Any) -> bool:
        if queue_id == broken_entry.queue_id:
            raise RuntimeError("simulated queue read failure")
        return real_get_cancel_requested(queue_root_arg, queue_id, **kwargs)

    with patch.object(queue_worker_mod, "get_cancel_requested", side_effect=get_cancel_requested):
        worker._shutdown_all()

    assert len(worker._running) == 0
    # Handled at the engine layer: the ordinary termination path ran for both jobs.
    assert fake_children.stopped(broken_child) and fake_children.stopped(live_child)
    assert broken_child.poll_result == -signal.SIGTERM
    assert queue_statuses(queue_root) == {
        broken_entry.queue_id: QueueStatus.PENDING,
        live_entry.queue_id: QueueStatus.PENDING,
    }


def test_shutdown_finalizes_a_child_whose_row_is_already_terminal(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The child recorded its own rejection on the row (crash recovery) and
    # exited 1 without writing a run state; the row is terminal, so the
    # completion path (a no-op mark plus slot release) applies, not requeue.
    rxn = queue_root / "mol_shut_row_terminal"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    assert mark_failed(
        queue_root,
        entry.queue_id,
        error="crash recovery rejected: simulated",
        expected_task_id=entry.task_id,
    )
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=1), "slot_row_terminal", task_id=None
    )

    worker._shutdown_all()

    assert entry.queue_id not in worker._running
    row = queue_row(queue_root, entry.queue_id)
    assert row.status == QueueStatus.FAILED
    # The completion path really ran: the replay marker was consumed and
    # the failed run state was written for the row's task.
    assert terminal_replay_marker_from_entry(row) is None
    written = load_state(rxn)
    assert written is not None
    assert (written["status"], written["job_id"]) == ("failed", entry.task_id)


def test_shutdown_tolerated_cancel_read_still_honors_a_pending_cancel(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The claim the tolerance rests on: with the cancel flag unreadable,
    # the ordinary requeue still turns a cancel-requested row into
    # cancelled (with its replay marker) rather than pending.
    rxn = queue_root / "mol_shut_pre_raise_cancel"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(), token, task_id=None
    )

    with patch.object(
        queue_worker_mod,
        "get_cancel_requested",
        side_effect=RuntimeError("simulated queue read failure"),
    ):
        worker._shutdown_all()

    assert entry.queue_id not in worker._running
    assert len(list_slots(admission_dir(queue_root))) == 0
    row = queue_row(queue_root, entry.queue_id)
    assert (row.status, row.cancel_requested) == (QueueStatus.CANCELLED, False)
    assert terminal_replay_marker_from_entry(row) is not None


def test_shutdown_ignores_a_terminal_state_left_by_an_earlier_task(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # A completed state from a previous submission in the same directory
    # does not conclude the current child's run.
    rxn = queue_root / "mol_shut_stale_state"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    save_child_state(
        rxn,
        "task-from-an-earlier-submission",
        "completed",
        {"status": "completed", "reason": "normal_termination"},
    )
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=0), "slot_stale_state", task_id=None
    )

    worker._shutdown_all()

    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.PENDING}


def test_shutdown_requeues_a_child_killed_mid_run(
    worker: OrcaQueueWorker,
    sleeping_child: Callable[[], subprocess.Popen[bytes]],
    queue_root: Path,
) -> None:
    # A child killed by a signal (SIGKILL after the grace window, or a death
    # before its stop handler was installed) exits negative and keeps the
    # resume path.
    rxn = queue_root / "mol_shut_killed"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    child = sleeping_child()
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, child, "slot_shut_killed", task_id=None
    )

    worker._shutdown_all()

    assert child.poll() == -signal.SIGTERM
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.PENDING}
