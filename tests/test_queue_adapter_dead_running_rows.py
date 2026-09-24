"""RUNNING rows left by a dead worker: the worker reconciles them, a submission does not sweep.

``adapter.enqueue`` no longer reconciles every running row of the queue root. It
recovers at most the rows of the directory being submitted, and only under the
worker's own protections (no live worker, no live admission slot, no held
``run.lock``, matching state generation).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orca_auto.core.admission import AdmissionStore, reserve_slot, update_slot_metadata
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.queue.worker.pid_file import WORKER_PID_FILE_NAME, write_worker_pid_file
from orca_auto.core.statuses import STATUS_PENDING, STATUS_RUNNING
from orca_auto.orca.queue import adapter
from orca_auto.orca.queue.adapter import DuplicateEntryError, enqueue, list_queue
from orca_auto.orca.queue.orphans import reconcile_dead_running_rows_for_dir
from orca_auto.orca.queue.terminal_replay import terminal_replay_marker_from_entry
from orca_auto.orca.run_lock import acquire_run_lock
from orca_auto.orca.run_snapshot import RunSnapshot
from orca_auto.orca.state import finalize_state, new_state
from orca_auto.orca.statuses import RunStatus
from tests.conftest import claim_next_entry


def _running_row(root: Path, name: str) -> tuple[Path, QueueEntry]:
    """A row a worker dequeued and whose worker then died: RUNNING, no lock, no slot."""
    rxn = root / name
    rxn.mkdir()
    entry = enqueue(root, str(rxn))
    dequeued = claim_next_entry(root)
    assert dequeued is not None and dequeued.queue_id == entry.queue_id
    return rxn, dequeued


def _row(root: Path, queue_id: str) -> QueueEntry:
    return next(entry for entry in list_queue(root) if entry.queue_id == queue_id)


def _snapshot(rxn: Path, status: str) -> RunSnapshot:
    return RunSnapshot(
        key=str(rxn),
        name=rxn.name,
        reaction_dir=rxn,
        run_id="",
        status=status,
        started_at="",
        updated_at="",
        completed_at="",
        selected_inp_name="",
        attempts=0,
    )


def test_dead_running_row_lists_as_pending_until_a_worker_reconciles_it(tmp_path: Path) -> None:
    from orca_auto.orca.run_status import queue_entry_status

    rxn, entry = _running_row(tmp_path, "rxn")

    # Running row, no run.lock, no worker: the listing does not claim it is running.
    assert queue_entry_status(adapter, entry, None) == STATUS_PENDING
    assert queue_entry_status(adapter, entry, _snapshot(rxn, STATUS_RUNNING)) == STATUS_PENDING
    # The row itself is left for the worker; the listing did not rewrite it.
    assert _row(tmp_path, entry.queue_id).status is QueueStatus.RUNNING

    # A child that holds run.lock is genuinely running.
    with acquire_run_lock(rxn):
        assert queue_entry_status(adapter, entry, None) == STATUS_RUNNING


def test_submission_never_sweeps_running_rows_of_other_directories(tmp_path: Path) -> None:
    _rxn_a, row_a = _running_row(tmp_path, "a")
    _rxn_b, row_b = _running_row(tmp_path, "b")
    fresh = tmp_path / "c"
    fresh.mkdir()

    enqueue(tmp_path, str(fresh), admission_root=tmp_path)

    assert _row(tmp_path, row_a.queue_id).status is QueueStatus.RUNNING
    assert _row(tmp_path, row_b.queue_id).status is QueueStatus.RUNNING


def test_resubmitting_a_dead_running_directory_requeues_only_that_row(tmp_path: Path) -> None:
    rxn_a, row_a = _running_row(tmp_path, "a")
    _rxn_b, row_b = _running_row(tmp_path, "b")

    with pytest.raises(DuplicateEntryError, match="status=pending"):
        enqueue(tmp_path, str(rxn_a), admission_root=tmp_path)

    requeued = _row(tmp_path, row_a.queue_id)
    assert requeued.status is QueueStatus.PENDING
    assert requeued.started_at == ""
    assert _row(tmp_path, row_b.queue_id).status is QueueStatus.RUNNING


def test_resubmitting_after_the_child_finished_closes_the_old_row_for_worker_replay(
    tmp_path: Path,
) -> None:
    rxn, row = _running_row(tmp_path, "a")
    state = new_state(rxn, rxn / "a.inp")
    state["job_id"] = row.task_id
    finalize_state(
        rxn,
        state,
        status=RunStatus.COMPLETED.value,
        final_result={"status": "completed", "completed_at": "2026-09-24T00:00:00+00:00"},
    )

    # The state file closes the row honestly (completed, with its run_id). Its
    # terminal side effects still belong to the worker, so the durable replay
    # marker fences a successor until a worker has replayed them; the user is
    # told that publication, not the calculation, is what is pending.
    with pytest.raises(DuplicateEntryError, match="status=completed"):
        enqueue(tmp_path, str(rxn), admission_root=tmp_path)

    closed = _row(tmp_path, row.queue_id)
    assert closed.status is QueueStatus.COMPLETED
    assert closed.metadata["run_id"] == state["run_id"]
    assert terminal_replay_marker_from_entry(closed) is not None
    assert [entry.queue_id for entry in list_queue(tmp_path)] == [row.queue_id]


def test_submission_without_admission_root_leaves_the_row_to_the_worker(tmp_path: Path) -> None:
    rxn, row = _running_row(tmp_path, "a")

    with pytest.raises(DuplicateEntryError, match="status=running"):
        enqueue(tmp_path, str(rxn))

    assert _row(tmp_path, row.queue_id).status is QueueStatus.RUNNING


def test_live_worker_keeps_the_submitter_out_of_running_rows(tmp_path: Path) -> None:
    rxn, row = _running_row(tmp_path, "a")
    write_worker_pid_file(tmp_path, WORKER_PID_FILE_NAME)

    assert reconcile_dead_running_rows_for_dir(tmp_path, str(rxn), admission_root=tmp_path) == 0
    with pytest.raises(DuplicateEntryError, match="status=running"):
        enqueue(tmp_path, str(rxn), admission_root=tmp_path)

    assert _row(tmp_path, row.queue_id).status is QueueStatus.RUNNING


@pytest.mark.parametrize("scoped", [True, False], ids=["work_dir_key", "queue_id_only"])
def test_child_holding_a_slot_but_not_yet_run_lock_is_never_requeued_by_a_submission(
    tmp_path: Path,
    scoped: bool,
) -> None:
    # The child has started (its slot is attached to the row) but has not yet
    # taken run.lock. A concurrent submission must treat it as live.
    rxn, row = _running_row(tmp_path, "a")
    token = reserve_slot(tmp_path, 4, source="queue_worker", state="reserved")
    assert token is not None
    assert update_slot_metadata(
        tmp_path,
        token,
        state="active",
        queue_id=row.queue_id,
        work_dir=str(rxn) if scoped else None,
        owner_pid=os.getpid(),
    )
    admission_before = AdmissionStore.for_root(tmp_path).path.read_bytes()

    assert reconcile_dead_running_rows_for_dir(tmp_path, str(rxn), admission_root=tmp_path) == 0
    with pytest.raises(DuplicateEntryError, match="status=running"):
        enqueue(tmp_path, str(rxn), admission_root=tmp_path)

    assert _row(tmp_path, row.queue_id).status is QueueStatus.RUNNING
    # The submitter reads admission state; it never rewrites it.
    assert AdmissionStore.for_root(tmp_path).path.read_bytes() == admission_before


def test_child_holding_run_lock_is_never_requeued_by_a_submission(tmp_path: Path) -> None:
    rxn, row = _running_row(tmp_path, "a")

    with acquire_run_lock(rxn):
        assert reconcile_dead_running_rows_for_dir(tmp_path, str(rxn), admission_root=tmp_path) == 0
        with pytest.raises(DuplicateEntryError, match="status=running"):
            enqueue(tmp_path, str(rxn), admission_root=tmp_path)

    assert _row(tmp_path, row.queue_id).status is QueueStatus.RUNNING


def _corrupt_admission_file(root: Path) -> Path:
    path = AdmissionStore.for_root(root).path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    return path


def test_fresh_directory_enqueues_although_the_admission_file_is_corrupt(tmp_path: Path) -> None:
    # The queue is consulted first; a directory without a RUNNING row of its
    # own never reads admission_slots.json, so its damage cannot block it.
    _rxn, row = _running_row(tmp_path, "a")
    _corrupt_admission_file(tmp_path)
    fresh = tmp_path / "fresh"
    fresh.mkdir()

    entry = enqueue(tmp_path, str(fresh), admission_root=tmp_path)

    assert entry.status is QueueStatus.PENDING
    assert _row(tmp_path, row.queue_id).status is QueueStatus.RUNNING


def test_dead_running_row_with_a_corrupt_admission_file_fails_closed_with_a_hint(
    tmp_path: Path,
) -> None:
    from orca_auto.orca.queue.orphans import DeadRunningRowUnjudgeableError

    rxn, row = _running_row(tmp_path, "a")
    corrupt = _corrupt_admission_file(tmp_path)

    with pytest.raises(DeadRunningRowUnjudgeableError, match="admission_slots.json") as info:
        enqueue(tmp_path, str(rxn), admission_root=tmp_path)

    assert isinstance(info.value, ValueError)
    assert str(corrupt) in str(info.value)
    assert "RUNNING" in str(info.value)
    # Nothing was decided for the row, and the damaged file was left as is.
    assert _row(tmp_path, row.queue_id).status is QueueStatus.RUNNING
    assert corrupt.read_text(encoding="utf-8") == "{not json"
