import json
from dataclasses import replace
from pathlib import Path

import pytest

from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.queue.worker.pid_file import write_worker_pid_file
from orca_auto.orca.queue import adapter as queue_adapter
from orca_auto.orca.queue import roots as queue_roots_mod
from orca_auto.orca.queue.adapter import (
    TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY,
    TERMINAL_REPLAY_METADATA_KEY,
    DuplicateEntryError,
    cancel,
    enqueue,
    get_active_entry_for_reaction_dir,
    get_cancel_requested,
    list_queue,
    mark_cancelled,
    mark_completed,
    mark_failed,
    queue_entry_force,
    queue_entry_reaction_dir,
    queue_entry_run_id,
    reconcile_orphaned_running_entries,
    requeue_running_entry,
    update_metadata,
)
from orca_auto.orca.run_cleanup import clear_terminal_queue_entries
from orca_auto.orca.state_reading import load_state, report_json_path
from tests.conftest import claim_next_entry, make_app_cfg, write_run_state
from tests.engine_artifact_helpers import orca_artifact_payload


def _find_entry(root: Path, queue_id: str) -> QueueEntry | None:
    for entry in list_queue(root):
        if entry.queue_id == queue_id:
            return entry
    return None


def _finish_terminal_replay(root: Path, queue_id: str) -> None:
    """Model the worker completing side effects and clearing its replay claim."""
    assert update_metadata(root, queue_id, {TERMINAL_REPLAY_METADATA_KEY: None})


def _write_completed_report(reaction_dir: Path, *, job_id: str, run_id: str) -> None:
    """A root-level (unverified) completed machine observation for ``reaction_dir``."""
    report_json_path(reaction_dir).write_text(
        json.dumps(
            orca_artifact_payload(
                job_id=job_id,
                run_id=run_id,
                reaction_dir=str(reaction_dir),
                status="completed",
                final_result={
                    "status": "completed",
                    "completed_at": "2026-03-10T04:59:59+00:00",
                },
            )
        ),
        encoding="utf-8",
    )


def _write_completed_generation(
    reaction_dir: Path, *, job_id: str, inp_name: str, reason: str, completed_at: str
) -> str:
    """Persist a completed ``job_state.json`` for ``job_id`` and return its run id."""
    state = write_run_state(
        reaction_dir,
        status="completed",
        job_id=job_id,
        selected_inp=reaction_dir / inp_name,
        final_result={"status": "completed", "reason": reason, "completed_at": completed_at},
    )
    return str(state["run_id"])


# -- enqueue / basic flow ---------------------------------------------------


def test_enqueue_creates_entry(queue_root: Path) -> None:
    entry = enqueue(queue_root, str(queue_root / "mol_A"))
    assert entry.status == QueueStatus.PENDING
    assert entry.queue_id.startswith("q_")
    assert entry.app_name == "orca_auto_orca"
    assert entry.task_id.startswith("orca_")
    assert entry.task_kind == "orca_run_inp"
    assert entry.engine == "orca"
    assert entry.priority == 10
    assert entry.metadata["reaction_dir"] == queue_entry_reaction_dir(entry)
    assert not entry.metadata["force"]


def test_enqueue_writes_queue_file(queue_root: Path) -> None:
    enqueue(queue_root, str(queue_root / "mol_A"))
    qp = queue_root / "queue.json"
    assert qp.exists()
    entries = json.loads(qp.read_text(encoding="utf-8"))
    assert len(entries) == 1


def test_enqueue_retries_generated_queue_and_task_id_collisions(
    queue_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = {
        "q": iter(["q_same", "q_same", "q_unique"]),
        "orca": iter(["orca_same", "orca_same", "orca_unique"]),
    }

    def next_token(prefix: str) -> str:
        return next(generated[prefix])

    monkeypatch.setattr(queue_adapter, "timestamped_token", next_token)
    first = enqueue(queue_root, str(queue_root / "mol_A"))
    second = enqueue(queue_root, str(queue_root / "mol_B"))

    assert (first.queue_id, first.task_id) == ("q_same", "orca_same")
    assert (second.queue_id, second.task_id) == ("q_unique", "orca_unique")
    assert [(entry.queue_id, entry.task_id) for entry in list_queue(queue_root)] == [
        ("q_same", "orca_same"),
        ("q_unique", "orca_unique"),
    ]


def test_enqueue_permanent_generated_id_collision_preserves_queue(
    queue_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(queue_adapter, "timestamped_token", lambda prefix: f"{prefix}_same")
    first = enqueue(queue_root, str(queue_root / "mol_A"))
    queue_path = queue_root / "queue.json"
    original = queue_path.read_bytes()
    with pytest.raises(RuntimeError, match="unique q token"):
        enqueue(queue_root, str(queue_root / "mol_B"))

    assert queue_path.read_bytes() == original
    [remaining] = list_queue(queue_root)
    assert remaining.queue_id == first.queue_id
    assert "mol_A" in queue_entry_reaction_dir(remaining)


def test_list_queue_empty(queue_root: Path) -> None:
    assert list_queue(queue_root) == []


def test_list_queue_rejects_corrupt_queue_file(queue_root: Path) -> None:
    qp = queue_root / "queue.json"
    qp.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(queue_store.QueueStoreCorruptError):
        list_queue(queue_root)


def test_enqueue_rejects_corrupt_queue_file_without_overwriting(queue_root: Path) -> None:
    qp = queue_root / "queue.json"
    corrupt_text = "{not valid json"
    qp.write_text(corrupt_text, encoding="utf-8")

    with pytest.raises(queue_store.QueueStoreCorruptError):
        enqueue(queue_root, str(queue_root / "mol_A"))

    assert qp.read_text(encoding="utf-8") == corrupt_text


def test_list_queue_with_filter(queue_root: Path) -> None:
    enqueue(queue_root, str(queue_root / "mol_A"))
    enqueue(queue_root, str(queue_root / "mol_B"))
    claim_next_entry(queue_root)  # mol_A → running
    assert len(list_queue(queue_root, status_filter="pending")) == 1
    assert len(list_queue(queue_root, status_filter="running")) == 1


# -- duplicate prevention ---------------------------------------------------


def test_duplicate_active_entry_blocked(queue_root: Path) -> None:
    """Pending/running entries for the same dir are always blocked."""
    reaction_dir = str(queue_root / "mol_A")
    entry = enqueue(queue_root, reaction_dir)
    with pytest.raises(DuplicateEntryError) as ctx:
        enqueue(queue_root, reaction_dir)
    assert str(ctx.value) == (
        f"Reaction directory already queued: {queue_entry_reaction_dir(entry)} "
        f"(queue_id={entry.queue_id}, status=pending). "
        "Wait for the active generation or its terminal publication to finish first."
    )


def test_duplicate_running_entry_blocked(queue_root: Path) -> None:
    enqueue(queue_root, str(queue_root / "mol_A"))
    claim_next_entry(queue_root)  # → running
    with pytest.raises(DuplicateEntryError):
        enqueue(queue_root, str(queue_root / "mol_A"))


def test_duplicate_terminal_with_pending_replay_is_blocked(queue_root: Path) -> None:
    """A terminal queue mark still blocks while publication replay is pending."""
    entry = enqueue(queue_root, str(queue_root / "mol_A"))
    mark_completed(queue_root, entry.queue_id)
    with pytest.raises(DuplicateEntryError):
        enqueue(queue_root, str(queue_root / "mol_A"))


def test_closed_terminal_generation_allows_same_directory_without_force(
    queue_root: Path,
) -> None:
    entry = enqueue(queue_root, str(queue_root / "mol_A"))
    mark_completed(queue_root, entry.queue_id)
    _finish_terminal_replay(queue_root, entry.queue_id)

    new_entry = enqueue(queue_root, str(queue_root / "mol_A"))

    assert entry.queue_id != new_entry.queue_id
    assert not queue_entry_force(new_entry)


def test_administratively_fenced_terminal_generation_blocks_successor(queue_root: Path) -> None:
    reaction_dir = str(queue_root / "mol_A")
    entry = enqueue(queue_root, reaction_dir)
    assert mark_failed(
        queue_root,
        entry.queue_id,
        error="administrative_fence",
        publish_terminal_side_effects=False,
    )
    [fenced] = list_queue(queue_root)
    assert fenced.metadata[TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY] is True

    with pytest.raises(DuplicateEntryError):
        enqueue(queue_root, reaction_dir)
    with pytest.raises(DuplicateEntryError):
        enqueue(queue_root, reaction_dir, force=True)


def test_duplicate_terminal_with_force_allowed(queue_root: Path) -> None:
    """Completed/failed entries allow re-enqueue with --force (intentional retry)."""
    entry = enqueue(queue_root, str(queue_root / "mol_A"))
    mark_completed(queue_root, entry.queue_id)
    with pytest.raises(DuplicateEntryError):
        enqueue(queue_root, str(queue_root / "mol_A"), force=True)

    _finish_terminal_replay(queue_root, entry.queue_id)
    new_entry = enqueue(queue_root, str(queue_root / "mol_A"), force=True)
    assert entry.queue_id != new_entry.queue_id
    assert queue_entry_force(new_entry)


def test_duplicate_active_blocked_even_with_force(queue_root: Path) -> None:
    """Active (pending/running) entries are always blocked, even with force."""
    enqueue(queue_root, str(queue_root / "mol_A"))
    with pytest.raises(DuplicateEntryError):
        enqueue(queue_root, str(queue_root / "mol_A"), force=True)


# -- dequeue ----------------------------------------------------------------


def test_dequeue_returns_highest_priority(queue_root: Path) -> None:
    enqueue(queue_root, str(queue_root / "low"), priority=20)
    enqueue(queue_root, str(queue_root / "high"), priority=1)
    enqueue(queue_root, str(queue_root / "mid"), priority=10)

    entry = claim_next_entry(queue_root)
    assert entry is not None
    assert "high" in queue_entry_reaction_dir(entry)
    assert entry.status == QueueStatus.RUNNING
    assert entry.started_at


def test_dequeue_empty_returns_none(queue_root: Path) -> None:
    assert claim_next_entry(queue_root) is None


# -- cancel -----------------------------------------------------------------


def test_cancel_pending(queue_root: Path) -> None:
    entry = enqueue(queue_root, str(queue_root / "mol_A"))
    result = cancel(queue_root, entry.queue_id)
    assert result is not None
    assert result.status == QueueStatus.CANCELLED


def test_cancel_running_sets_flag(queue_root: Path) -> None:
    entry = enqueue(queue_root, str(queue_root / "mol_A"))
    claim_next_entry(queue_root)
    result = cancel(queue_root, entry.queue_id)
    assert result is not None
    assert result.cancel_requested
    assert get_cancel_requested(queue_root, entry.queue_id)


def test_cancel_terminal_returns_none(queue_root: Path) -> None:
    entry = enqueue(queue_root, str(queue_root / "mol_A"))
    mark_completed(queue_root, entry.queue_id)
    assert cancel(queue_root, entry.queue_id) is None


# -- mark_completed / mark_failed -------------------------------------------


def test_mark_completed(queue_root: Path) -> None:
    entry = enqueue(queue_root, str(queue_root / "mol_A"))
    claim_next_entry(queue_root)
    assert mark_completed(queue_root, entry.queue_id, run_id="run_test")
    found = _find_entry(queue_root, entry.queue_id)
    assert found is not None
    assert found.status == QueueStatus.COMPLETED
    assert queue_entry_run_id(found) == "run_test"


def test_mark_failed_with_error(queue_root: Path) -> None:
    entry = enqueue(queue_root, str(queue_root / "mol_A"))
    claim_next_entry(queue_root)
    assert mark_failed(queue_root, entry.queue_id, error="exit_code=1")
    found = _find_entry(queue_root, entry.queue_id)
    assert found is not None
    assert found.status == QueueStatus.FAILED
    assert found.error == "exit_code=1"


# -- clear / count ----------------------------------------------------------


def test_clear_terminal(queue_root: Path) -> None:
    e1 = enqueue(queue_root, str(queue_root / "a"))
    e2 = enqueue(queue_root, str(queue_root / "b"))
    enqueue(queue_root, str(queue_root / "c"))  # stays pending
    mark_completed(queue_root, e1.queue_id)
    mark_failed(queue_root, e2.queue_id)

    assert clear_terminal_queue_entries(queue_root) == (0, 0)
    _finish_terminal_replay(queue_root, e1.queue_id)
    _finish_terminal_replay(queue_root, e2.queue_id)
    assert clear_terminal_queue_entries(queue_root) == (2, 0)
    remaining = list_queue(queue_root)
    assert len(remaining) == 1
    assert remaining[0].status == QueueStatus.PENDING


def test_list_queue_can_count_running(queue_root: Path) -> None:
    enqueue(queue_root, str(queue_root / "a"))
    enqueue(queue_root, str(queue_root / "b"))
    claim_next_entry(queue_root)
    claim_next_entry(queue_root)
    running = [entry for entry in list_queue(queue_root) if entry.status == QueueStatus.RUNNING]
    assert len(running) == 2


def test_get_active_entry_for_reaction_dir_returns_pending(queue_root: Path) -> None:
    reaction_dir = queue_root / "pending_lookup"
    entry = enqueue(queue_root, str(reaction_dir))
    found = get_active_entry_for_reaction_dir(queue_root, str(reaction_dir))
    assert found is not None
    assert found.queue_id == entry.queue_id


def test_get_active_entry_for_reaction_dir_returns_running(queue_root: Path) -> None:
    reaction_dir = queue_root / "running_lookup"
    entry = enqueue(queue_root, str(reaction_dir))
    claim_next_entry(queue_root)
    found = get_active_entry_for_reaction_dir(queue_root, str(reaction_dir))
    assert found is not None
    assert found.queue_id == entry.queue_id


def test_get_active_entry_for_reaction_dir_ignores_terminal_entry(queue_root: Path) -> None:
    reaction_dir = queue_root / "terminal_lookup"
    entry = enqueue(queue_root, str(reaction_dir))
    mark_completed(queue_root, entry.queue_id)
    assert get_active_entry_for_reaction_dir(queue_root, str(reaction_dir)) is None


# -- reconcile orphaned running entries -------------------------------------


def test_reconcile_orphaned_running_ignores_root_report_only(queue_root: Path) -> None:
    reaction_dir = queue_root / "mol_done"
    reaction_dir.mkdir()
    entry = enqueue(queue_root, str(reaction_dir))
    claim_next_entry(queue_root)

    _write_completed_report(reaction_dir, job_id=entry.task_id, run_id="run_done_1")

    changed = reconcile_orphaned_running_entries(queue_root)
    assert changed == 1

    found = _find_entry(queue_root, entry.queue_id)
    assert found is not None
    assert found.status == QueueStatus.PENDING
    assert queue_entry_run_id(found) is None
    assert found.finished_at == ""


def test_reconcile_orphaned_force_entry_ignores_previous_generation_state(
    queue_root: Path,
) -> None:
    reaction_dir = queue_root / "mol_force_state"
    _write_completed_generation(
        reaction_dir,
        job_id="task-a",
        inp_name="job.inp",
        reason="normal_termination",
        completed_at="2026-03-10T04:59:59+00:00",
    )
    current = enqueue(queue_root, str(reaction_dir), force=True, task_id="task-b")
    claim_next_entry(queue_root)

    changed = reconcile_orphaned_running_entries(queue_root)

    assert changed == 1
    found = _find_entry(queue_root, current.queue_id)
    assert found is not None
    assert found.status == QueueStatus.PENDING
    assert queue_entry_run_id(found) is None


def test_reconcile_orphaned_force_same_task_requires_run_identity(queue_root: Path) -> None:
    reaction_dir = queue_root / "mol_force_same_task"
    previous_run_id = _write_completed_generation(
        reaction_dir,
        job_id="task-same",
        inp_name="old.inp",
        reason="previous_generation",
        completed_at="2026-03-10T04:59:59+00:00",
    )
    previous_entry = enqueue(queue_root, str(reaction_dir), task_id="task-same")
    claim_next_entry(queue_root)
    mark_completed(queue_root, previous_entry.queue_id, run_id=previous_run_id)
    _finish_terminal_replay(queue_root, previous_entry.queue_id)
    current = enqueue(queue_root, str(reaction_dir), force=True, task_id="task-same")
    claim_next_entry(queue_root)

    changed = reconcile_orphaned_running_entries(queue_root)

    assert changed == 1
    found = _find_entry(queue_root, current.queue_id)
    assert found is not None
    assert found.status == QueueStatus.PENDING
    assert queue_entry_run_id(found) is None
    persisted = load_state(reaction_dir)
    assert persisted is not None
    assert persisted["run_id"] == previous_run_id
    assert persisted["status"] == "completed"


def test_reconcile_orphaned_force_same_task_accepts_new_run_identity(queue_root: Path) -> None:
    reaction_dir = queue_root / "mol_force_same_task_current"
    previous_run_id = _write_completed_generation(
        reaction_dir,
        job_id="task-same",
        inp_name="old.inp",
        reason="previous_generation",
        completed_at="2026-03-10T04:59:59+00:00",
    )
    previous_entry = enqueue(queue_root, str(reaction_dir), task_id="task-same")
    claim_next_entry(queue_root)
    mark_completed(queue_root, previous_entry.queue_id, run_id=previous_run_id)
    _finish_terminal_replay(queue_root, previous_entry.queue_id)
    current = enqueue(queue_root, str(reaction_dir), force=True, task_id="task-same")
    claim_next_entry(queue_root)
    current_run_id = _write_completed_generation(
        reaction_dir,
        job_id="task-same",
        inp_name="current.inp",
        reason="current_generation",
        completed_at="2026-03-11T04:59:59+00:00",
    )

    changed = reconcile_orphaned_running_entries(queue_root)

    assert changed == 1
    found = _find_entry(queue_root, current.queue_id)
    assert found is not None
    assert found.status == QueueStatus.COMPLETED
    assert queue_entry_run_id(found) == current_run_id


def test_reconcile_orphaned_force_same_task_rejects_prior_report_run(queue_root: Path) -> None:
    reaction_dir = queue_root / "mol_force_same_task_report"
    reaction_dir.mkdir()
    previous_run_id = "run-previous"
    previous_entry = enqueue(queue_root, str(reaction_dir), task_id="task-same")
    claim_next_entry(queue_root)
    mark_completed(queue_root, previous_entry.queue_id, run_id=previous_run_id)
    _finish_terminal_replay(queue_root, previous_entry.queue_id)
    _write_completed_report(reaction_dir, job_id="task-same", run_id=previous_run_id)
    current = enqueue(queue_root, str(reaction_dir), force=True, task_id="task-same")
    claim_next_entry(queue_root)

    changed = reconcile_orphaned_running_entries(queue_root)

    assert changed == 1
    found = _find_entry(queue_root, current.queue_id)
    assert found is not None
    assert found.status == QueueStatus.PENDING
    assert queue_entry_run_id(found) is None


def test_reconcile_orphaned_force_entry_ignores_previous_generation_report(
    queue_root: Path,
) -> None:
    reaction_dir = queue_root / "mol_force_report"
    reaction_dir.mkdir()
    _write_completed_report(reaction_dir, job_id="task-a", run_id="run-a")
    current = enqueue(queue_root, str(reaction_dir), force=True, task_id="task-b")
    claim_next_entry(queue_root)

    changed = reconcile_orphaned_running_entries(queue_root)

    assert changed == 1
    found = _find_entry(queue_root, current.queue_id)
    assert found is not None
    assert found.status == QueueStatus.PENDING
    assert queue_entry_run_id(found) is None


def test_orca_engine_dequeue_skips_foreign_engine_entries(queue_root: Path) -> None:
    # The ORCA worker shares the runs root with another app's jobs. Its
    # claim must skip a foreign-engine entry; otherwise the ORCA worker
    # claims and mis-runs the foreign job.
    orca_entry = enqueue(queue_root, str(queue_root / "orca_job"))
    foreign = replace(
        orca_entry,
        queue_id="q_other_1",
        app_name="orca_auto_other",
        engine="other",
        priority=1,  # higher priority than the ORCA entry -> claimed first if unfiltered
        metadata={**orca_entry.metadata, "reaction_dir": str(queue_root / "other_job")},
    )
    queue_store.save_entries(queue_root, [foreign, orca_entry])

    cfg = make_app_cfg(queue_root)

    claimed = queue_roots_mod.dequeue_next_entry(cfg)
    assert claimed is not None
    assert claimed == (queue_root.resolve(), claimed[1])
    assert claimed[1].queue_id == orca_entry.queue_id
    # The foreign entry is left unclaimed by the ORCA worker.
    assert queue_roots_mod.dequeue_next_entry(cfg) is None


def test_reconcile_honors_cancel_requested_orphan(queue_root: Path) -> None:
    # A running job that was cancel-requested and then lost its worker (no
    # run.lock, no terminal state, no job_report) must be reconciled to a
    # terminal CANCELLED state, not re-queued to PENDING where dequeue would
    # skip it forever (cancel_requested entries are never dequeued).
    reaction_dir = queue_root / "mol_cancel"
    reaction_dir.mkdir()
    entry = enqueue(queue_root, str(reaction_dir))
    claim_next_entry(queue_root)  # -> RUNNING
    cancel(queue_root, entry.queue_id)  # cancel_requested=True, stays RUNNING

    running = _find_entry(queue_root, entry.queue_id)
    assert running is not None
    assert running.status == QueueStatus.RUNNING
    assert running.cancel_requested

    changed = reconcile_orphaned_running_entries(queue_root)
    assert changed == 1

    found = _find_entry(queue_root, entry.queue_id)
    assert found is not None
    assert found.status == QueueStatus.CANCELLED
    assert not found.cancel_requested
    assert found.finished_at


def test_reconcile_skips_when_worker_pid_is_alive(queue_root: Path) -> None:
    reaction_dir = queue_root / "mol_done"
    reaction_dir.mkdir()
    entry = enqueue(queue_root, str(reaction_dir))
    claim_next_entry(queue_root)

    _write_completed_report(reaction_dir, job_id="run_done_1", run_id="run_done_1")
    write_worker_pid_file(queue_root)

    changed = reconcile_orphaned_running_entries(queue_root)

    assert changed == 0
    found = _find_entry(queue_root, entry.queue_id)
    assert found is not None
    assert found.status == QueueStatus.RUNNING


# -- queue lookup via list --------------------------------------------------


def test_lookup_entry_exists(queue_root: Path) -> None:
    entry = enqueue(queue_root, str(queue_root / "mol_A"))
    found = _find_entry(queue_root, entry.queue_id)
    assert found is not None
    assert found.queue_id == entry.queue_id


def test_lookup_entry_missing(queue_root: Path) -> None:
    assert _find_entry(queue_root, "q_nonexistent") is None


# -- priority tie-breaking by arrival (queue-file row) order ----------------


def test_fifo_on_same_priority(queue_root: Path) -> None:
    e1 = enqueue(queue_root, str(queue_root / "first"))
    enqueue(queue_root, str(queue_root / "second"))
    dequeued = claim_next_entry(queue_root)
    assert dequeued is not None
    assert dequeued.queue_id == e1.queue_id


# -- worker transitions -----------------------------------------------------


def test_mark_cancelled_updates_running_entry(queue_root: Path) -> None:
    reaction_dir = queue_root / "mol_cancelled"
    reaction_dir.mkdir()
    entry = enqueue(queue_root, str(reaction_dir))
    claim_next_entry(queue_root)

    updated = mark_cancelled(queue_root, entry.queue_id)

    assert updated
    queue_entries = list_queue(queue_root)
    assert queue_entries[0].status.value == "cancelled"
    assert not queue_entries[0].cancel_requested
    assert queue_entries[0].finished_at is not None


def test_requeue_running_entry_returns_job_to_pending(queue_root: Path) -> None:
    reaction_dir = queue_root / "mol_pending_again"
    reaction_dir.mkdir()
    entry = enqueue(queue_root, str(reaction_dir))
    claim_next_entry(queue_root)

    updated = requeue_running_entry(queue_root, entry.queue_id)

    assert updated
    queue_entries = list_queue(queue_root)
    assert queue_entries[0].status.value == "pending"
    assert queue_entries[0].started_at == ""
    assert not queue_entries[0].cancel_requested
