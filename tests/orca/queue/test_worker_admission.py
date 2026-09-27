"""``orca_auto.orca.queue.worker``: admission and child start."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from orca_auto.core.admission import admission_dir, list_slots, release_slot, reserve_slot
from orca_auto.core.queue.store import save_entries as save_entries_core
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.queue import worker as queue_worker_mod
from orca_auto.orca.queue.adapter import enqueue, list_queue
from orca_auto.orca.queue.entries import queue_entry_reaction_dir
from orca_auto.orca.queue.models import OrcaRunningJob
from orca_auto.orca.queue.worker import OrcaQueueWorker
from tests.conftest import claim_next_entry
from tests.queue_worker_helpers import (
    ChildStarter,
    FakeChildren,
    SpawnCall,
    admission_file_identity,
    current_orca_queue_metadata,
    job_record,
    queue_statuses,
    reserve_job_slot,
    running_job,
    write_completed_run_state,
)


def _command_arg(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


# ---------------------------------------------------------------------------
# Admission and child start
# ---------------------------------------------------------------------------


def test_fill_slots_empty_queue(worker: OrcaQueueWorker) -> None:
    worker._fill_slots()
    assert len(worker._running) == 0


def test_fill_slots_idle_poll_leaves_admission_file_untouched(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(start=child_starter)
    # Capacity must remain: at the limit main also skips the write, which
    # would make the assertion vacuous. One live slot out of two stays.
    token = reserve_slot(worker.admission_root, 2, source="queue_worker", state="reserved")
    assert token is not None
    assert worker.max_concurrent == 2
    assert len(list_slots(worker.admission_root)) == 1
    before = admission_file_identity(admission_dir(queue_root))

    status = worker._fill_slots()

    assert status == "idle"
    assert admission_file_identity(admission_dir(queue_root)) == before
    slots = json.loads(
        (admission_dir(queue_root) / "admission_slots.json").read_text(encoding="utf-8")
    )
    assert [slot["token"] for slot in slots] == [token]
    assert len(worker._running) == 0
    assert child_starter.started == []


def test_admission_reservation_moves_the_admission_file_to_a_new_inode(
    worker: OrcaQueueWorker, queue_root: Path
) -> None:
    # Positive control for the idle-poll probe above: a real reservation
    # write replaces the admission file, so an unchanged inode is evidence
    # that no reservation happened.
    first = reserve_slot(worker.admission_root, 2, source="probe", state="reserved")
    assert first is not None
    before = admission_file_identity(admission_dir(queue_root))
    second = reserve_slot(worker.admission_root, 2, source="probe", state="reserved")
    assert second is not None
    # The atomic replace may reuse the freed inode number, so the probe
    # the idle-poll test relies on is the (inode, mtime_ns) pair.
    assert admission_file_identity(admission_dir(queue_root)) != before
    assert len(list_slots(worker.admission_root)) == 2


def test_start_job(worker: OrcaQueueWorker, fake_popen: list[SpawnCall], queue_root: Path) -> None:
    entry = QueueEntry(
        queue_id="q_test",
        app_name="orca_auto_orca",
        task_id="task_test_123",
        task_kind="orca_run_inp",
        engine="orca",
        metadata={
            "reaction_dir": str(queue_root / "mol_A"),
            "force": False,
            "worker_log": "/tmp/unsafe-worker.log",
        },
    )
    token = reserve_slot(
        admission_dir(queue_root), worker.max_concurrent, source="queue_worker", state="reserved"
    )
    assert token is not None

    worker._start_job(queue_root, entry, admission_token=token)

    assert "q_test" in worker._running
    [spawned] = fake_popen
    assert worker._running["q_test"].process is spawned.process
    log_path = (queue_root / "logs" / "q_test.log").resolve()
    assert spawned.log_path == log_path
    assert log_path.exists()
    command = spawned.args
    assert "orca_auto.orca.commands.worker_child" in command
    assert "--engine" not in command
    assert _command_arg(command, "--queue-root") == str(queue_root)
    assert _command_arg(command, "--queue-id") == "q_test"
    assert _command_arg(command, "--admission-token") == token
    assert "--reaction-dir" not in command


def test_start_job_prefers_queue_metadata_for_tracking(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(start=child_starter)
    reaction_dir = queue_root / "mol_meta"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "rxn.inp"
    # Derived from this input the job would be an "opt" of molecule "rxn";
    # the queue row carries a different identity, which must win.
    selected_inp.write_text("! Opt\n", encoding="utf-8")
    entry = QueueEntry(
        queue_id="q_meta",
        app_name="orca_auto_orca",
        task_id="task_meta_123",
        task_kind="orca_run_inp",
        engine="orca",
        metadata={
            "reaction_dir": str(reaction_dir),
            "force": False,
            "selected_inp": str(selected_inp),
            "selected_input_xyz": str(selected_inp),
            "job_type": "freq",
            "molecule_key": "queue-H2",
            "resource_request": {"max_cores": 4, "max_memory_gb": 12},
            "resource_actual": {"max_cores": 3, "max_memory_gb": 11},
        },
    )
    token = reserve_slot(
        admission_dir(queue_root), worker.max_concurrent, source="queue_worker", state="reserved"
    )
    assert token is not None

    worker._start_job(queue_root, entry, admission_token=token)

    record = job_record(queue_root, "task_meta_123")
    assert record is not None
    assert record["status"] == "running"
    assert Path(record["original_run_dir"]) == reaction_dir.resolve()
    assert record["job_type"] == "orca_freq"
    assert record["selected_input_xyz"] == str(selected_inp)
    assert record["molecule_key"] == "queue-H2"
    assert record["resource_request"] == {"max_cores": 4, "max_memory_gb": 12}
    assert record["resource_actual"] == {"max_cores": 3, "max_memory_gb": 11}


def test_start_job_oserror(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    child_starter.error = OSError("spawn failed")
    worker = make_worker(start=child_starter)
    rxn = queue_root / "mol_err"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    claim_next_entry(queue_root)

    assert worker._start_job(queue_root, entry, admission_token=token) is False

    assert entry.queue_id not in worker._running
    assert len(list_slots(admission_dir(queue_root))) == 0
    [failed] = list_queue(queue_root)
    assert (failed.status, failed.error) == (QueueStatus.FAILED, "spawn failed")


def test_start_job_attach_error_releases_slot_and_terminates_process(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    queue_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The slot vanished between reservation and attach (an operator cleared
    # the admission file): the child must not run without admission.
    worker = make_worker(start=child_starter)
    rxn = queue_root / "mol_attach_err"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    token = reserve_job_slot(admission_dir(queue_root), worker.max_concurrent, entry, rxn)
    claim_next_entry(queue_root)
    release_slot(admission_dir(queue_root), token)
    release_calls: list[str] = []
    real_release = worker._release_admission_slot

    def counted_release(admission_token: str) -> object:
        release_calls.append(admission_token)
        return real_release(admission_token)

    monkeypatch.setattr(worker, "_release_admission_slot", counted_release)

    assert worker._start_job(queue_root, entry, admission_token=token) is False

    assert entry.queue_id not in worker._running
    [started] = child_starter.started
    assert fake_children.stopped(started.process)
    assert len(list_slots(admission_dir(queue_root))) == 0
    [updated] = list_queue(queue_root)
    assert updated.status == QueueStatus.FAILED
    # Handled exactly once: the specific refusal reason survives and the slot
    # is released once (a second release of the same token would return False).
    assert updated.error == "admission_slot_missing"
    assert release_calls == [token]


def test_start_error_does_not_fail_replacement_generation(
    worker: OrcaQueueWorker, queue_root: Path
) -> None:
    rxn = queue_root / "mol_start_error_replacement"
    rxn.mkdir()
    selected = enqueue(queue_root, str(rxn), task_id="task-a")
    running = claim_next_entry(queue_root)
    assert running is not None
    token = reserve_job_slot(admission_dir(queue_root), 2, selected, rxn)
    replacement = replace(running, task_id="task-b")
    save_entries_core(queue_root, [replacement])

    worker._mark_entry_failed_and_release(queue_root, running, token, error="worker start failed")

    [durable] = list_queue(queue_root)
    assert (durable.task_id, durable.status) == ("task-b", QueueStatus.RUNNING)
    assert len(list_slots(admission_dir(queue_root))) == 0


def test_fill_slots_starts_pending_jobs(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(start=child_starter)
    rxn = queue_root / "mol_A"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), metadata=current_orca_queue_metadata(rxn))

    worker._fill_slots()

    assert list(worker._running) == [entry.queue_id]
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.RUNNING}


def test_fill_slots_does_not_reclaim_a_row_whose_previous_job_is_still_tracked(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    # A child stopped by an external SIGTERM requeues its own row for resume
    # and only then exits; the requeue is applied directly here. Until the
    # parent has seen that exit and released the slot, the row must not start
    # a second job under the same queue id: that would replace the tracked
    # job and strand its admission slot.
    worker = make_worker(start=child_starter)
    rxn = queue_root / "mol_requeued"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), metadata=current_orca_queue_metadata(rxn))
    behind = queue_root / "mol_behind"
    behind.mkdir()

    worker._fill_slots()
    tracked = worker._running[entry.queue_id]
    assert queue_worker_mod.requeue_running_entry(queue_root, entry.queue_id)
    behind_entry = enqueue(queue_root, str(behind), metadata=current_orca_queue_metadata(behind))

    worker._fill_slots()

    assert len(child_starter.started) == 2
    assert worker._running[entry.queue_id] is tracked
    statuses = queue_statuses(queue_root)
    assert statuses[entry.queue_id] == QueueStatus.PENDING
    # The row behind it is unaffected and takes the free slot.
    assert statuses[behind_entry.queue_id] == QueueStatus.RUNNING
    assert worker._running[behind_entry.queue_id].process is child_starter.started[1].process


def test_fill_slots_with_only_a_tracked_pending_row_leaves_admission_untouched(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(start=child_starter)
    rxn = queue_root / "mol_requeued_only"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), metadata=current_orca_queue_metadata(rxn))
    worker._fill_slots()
    assert queue_worker_mod.requeue_running_entry(queue_root, entry.queue_id)
    before = admission_file_identity(admission_dir(queue_root))

    status = worker._fill_slots()

    assert status == "idle"
    assert admission_file_identity(admission_dir(queue_root)) == before
    assert len(child_starter.started) == 1
    [row] = list_queue(queue_root)
    assert row.status == QueueStatus.PENDING


def test_fill_slots_attaches_queue_identity_to_reserved_slot(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(max_concurrent=1, start=child_starter)
    rxn = queue_root / "mol_identity"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), metadata=current_orca_queue_metadata(rxn))

    worker._fill_slots()

    [slot] = list_slots(admission_dir(queue_root))
    [started] = child_starter.started
    assert slot.queue_id == entry.queue_id
    assert slot.app_name == entry.app_name
    assert slot.task_id == entry.task_id
    assert slot.state == "active"
    assert slot.owner_pid == started.process.pid
    assert slot.work_dir == str(rxn)


def test_fill_slots_preserves_task_id_across_slot_and_worker_handoff(
    make_worker: Callable[..., OrcaQueueWorker], fake_popen: list[SpawnCall], queue_root: Path
) -> None:
    worker = make_worker(max_concurrent=1)
    rxn = queue_root / "mol_task_identity"
    rxn.mkdir()
    entry = enqueue(
        queue_root,
        str(rxn),
        task_id="orca_task_preserved_123",
        metadata=current_orca_queue_metadata(rxn),
    )
    assert entry.queue_id != entry.task_id

    worker._fill_slots()

    [slot] = list_slots(admission_dir(queue_root))
    assert slot.queue_id == entry.queue_id
    assert slot.task_id == entry.task_id
    assert slot.queue_id != slot.task_id
    [spawned] = fake_popen
    assert spawned.log_path == (queue_root / "logs" / f"{entry.queue_id}.log").resolve()
    assert _command_arg(spawned.args, "--admission-token") == slot.token
    assert _command_arg(spawned.args, "--queue-id") == entry.queue_id
    assert "--admission-task-id" not in spawned.args
    assert "--admission-app-name" not in spawned.args


def test_fill_slots_respects_max_concurrent(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(max_concurrent=1, start=child_starter)
    for name in ("a", "b"):
        d = queue_root / name
        d.mkdir()
        enqueue(queue_root, str(d), metadata=current_orca_queue_metadata(d))

    worker._fill_slots()

    assert len(worker._running) == 1
    assert sorted(queue_statuses(queue_root).values(), key=str) == [
        QueueStatus.PENDING,
        QueueStatus.RUNNING,
    ]


def test_fill_slots_fills_all_available_capacity(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(max_concurrent=3, start=child_starter)
    for name in ("p1", "p2", "p3", "p4"):
        reaction_dir = queue_root / name
        reaction_dir.mkdir()
        enqueue(queue_root, str(reaction_dir), metadata=current_orca_queue_metadata(reaction_dir))

    worker._fill_slots()

    queue_by_name = {
        Path(queue_entry_reaction_dir(entry)).name: entry.status.value
        for entry in list_queue(queue_root)
    }
    assert len(worker._running) == 3
    assert len(child_starter.started) == 3
    assert queue_by_name == {"p1": "running", "p2": "running", "p3": "running", "p4": "pending"}


def test_fill_slots_refills_immediately_after_completion(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    queue_root: Path,
) -> None:
    worker = make_worker(max_concurrent=1, start=child_starter)
    first_dir = queue_root / "first"
    second_dir = queue_root / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    completed_entry = enqueue(
        queue_root,
        str(first_dir),
        task_id="task_terminal_123",
        metadata=current_orca_queue_metadata(first_dir),
    )
    pending_entry = enqueue(
        queue_root, str(second_dir), metadata=current_orca_queue_metadata(second_dir)
    )
    claim_next_entry(queue_root)
    write_completed_run_state(first_dir)
    token = reserve_job_slot(
        admission_dir(queue_root), worker.max_concurrent, completed_entry, first_dir
    )
    worker._running[completed_entry.queue_id] = running_job(
        worker, completed_entry, first_dir, fake_children.spawn(exited=0), token, task_id=None
    )

    worker._check_completed_jobs()
    worker._fill_slots()

    queue_by_name = {
        Path(queue_entry_reaction_dir(entry)).name: entry.status.value
        for entry in list_queue(queue_root)
    }
    assert len(child_starter.started) == 1
    assert list(worker._running) == [pending_entry.queue_id]
    assert queue_by_name == {"first": "completed", "second": "running"}


def test_fill_slots_respects_admission_slots_without_run_lock(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(max_concurrent=1, start=child_starter)
    queued = queue_root / "queued_only"
    queued.mkdir()
    entry = enqueue(queue_root, str(queued), metadata=current_orca_queue_metadata(queued))
    token = reserve_slot(
        worker.admission_root,
        1,
        work_dir=str(queue_root / "reserved_hold"),
        source="queue_worker",
        state="reserved",
    )
    assert token is not None

    worker._fill_slots()

    assert len(worker._running) == 0
    assert child_starter.started == []
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.PENDING}


def test_fill_slots_counts_existing_worker_admission_slot_once(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    queue_root: Path,
) -> None:
    worker = make_worker(max_concurrent=2, start=child_starter)
    active_dir = queue_root / "already_running"
    token = reserve_slot(
        admission_dir(queue_root),
        worker.max_concurrent,
        work_dir=str(active_dir),
        queue_id="q_existing",
        source="queue_worker",
        state="reserved",
    )
    assert token is not None
    worker._running["q_existing"] = OrcaRunningJob(
        queue_root=worker.queue_root,
        queue_id="q_existing",
        reaction_dir=str(active_dir),
        process=fake_children.spawn(),
        admission_token=token,
    )
    queued = queue_root / "queued_only"
    queued.mkdir()
    enqueue(queue_root, str(queued), metadata=current_orca_queue_metadata(queued))

    worker._fill_slots()

    assert len(worker._running) == 2
    assert len(child_starter.started) == 1
