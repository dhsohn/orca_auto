from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from orca_auto.core.artifacts import QUEUE_FILE
from orca_auto.core.queue import persistence as queue_persistence
from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_PREPARING,
    queue_record_sync_metadata,
)
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.queue.worker.pid_file import write_worker_pid_file
from orca_auto.core.utils import persistence as persistence_utils
from orca_auto.orca import run_cleanup
from orca_auto.orca.machine_observation import report_json_path
from orca_auto.orca.queue import adapter as queue_adapter
from orca_auto.orca.queue import entries as queue_entries
from orca_auto.orca.queue import orphans as queue_orphans
from orca_auto.orca.queue import roots as queue_roots_mod
from orca_auto.orca.queue.adapter import (
    DuplicateEntryError,
    cancel,
    enqueue,
    get_active_entry_for_reaction_dir,
    get_cancel_requested,
    list_queue,
    mark_cancelled,
    mark_completed,
    mark_failed,
    requeue_running_entry,
    update_metadata,
)
from orca_auto.orca.queue.entries import (
    TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY,
    TERMINAL_REPLAY_METADATA_KEY,
    queue_entry_force,
    queue_entry_reaction_dir,
    queue_entry_run_id,
)
from orca_auto.orca.queue.orphans import reconcile_orphaned_running_entries
from orca_auto.orca.queue.terminal_marker import terminal_replay_marker_from_entry
from orca_auto.orca.run_cleanup import clear_terminal_queue_entries
from orca_auto.orca.state_reading import load_state
from orca_auto.orca.statuses import RunStatus
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

    monkeypatch.setattr(persistence_utils, "timestamped_token", next_token)
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
    monkeypatch.setattr(persistence_utils, "timestamped_token", lambda prefix: f"{prefix}_same")
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
    queue_persistence.save_entries(queue_root, [foreign, orca_entry])

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


def _entry(
    queue_id: str,
    reaction_dir: str,
    status: str,
    *,
    priority: int = 10,
    started_at: str | None = None,
    finished_at: str | None = None,
    cancel_requested: bool = False,
    run_id: str | None = None,
    error: str | None = None,
) -> QueueEntry:
    entry: dict[str, Any] = {
        "queue_id": queue_id,
        "app_name": "orca_auto_orca",
        "task_id": queue_id,
        "task_kind": "orca_run_inp",
        "engine": "orca",
        "status": status,
        "priority": priority,
        "enqueued_at": "2026-03-10T00:00:00+00:00",
        "started_at": started_at or "",
        "finished_at": finished_at or "",
        "cancel_requested": cancel_requested,
        "error": error or "",
        "metadata": {
            "reaction_dir": reaction_dir,
            "force": False,
            **queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_COMPLETE,
                token=queue_id,
                owner_pid=0,
            ),
        },
    }
    if run_id is not None:
        entry["metadata"]["run_id"] = run_id
    return queue_persistence.entry_from_dict(entry)


def _save_entries(root: Path, entries: list[QueueEntry]) -> None:
    queue_persistence.save_entries(root, entries)


def _assert_terminal_replay_marker(
    entry: QueueEntry,
    *,
    status: QueueStatus,
    error: str,
) -> None:
    marker = terminal_replay_marker_from_entry(entry)
    assert marker is not None
    assert marker == entry.metadata[TERMINAL_REPLAY_METADATA_KEY]
    assert marker["task_id"] == entry.task_id
    assert marker["status"] == status.value
    assert marker["error"] == error


def _foreign_entry(
    queue_id: str,
    *,
    status: QueueStatus = QueueStatus.PENDING,
    reaction_dir: str = "",
) -> QueueEntry:
    return QueueEntry(
        queue_id=queue_id,
        app_name="orca_auto_other",
        task_id=f"other-{queue_id}",
        task_kind="other_sp",
        engine="other",
        status=status,
        priority=1,
        enqueued_at="2026-03-10T00:00:00+00:00",
        finished_at=(
            "2026-03-10T00:01:00+00:00"
            if status in {QueueStatus.COMPLETED, QueueStatus.FAILED, QueueStatus.CANCELLED}
            else ""
        ),
        metadata={
            "job_type": "sp",
            "job_dir": reaction_dir,
            "reaction_dir": reaction_dir,
            **queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_COMPLETE,
                token=queue_id,
                owner_pid=0,
            ),
        },
    )


def test_load_entries_cover_edge_cases(tmp_path: Path) -> None:
    assert queue_persistence.load_entries(tmp_path) == []

    queue_path = tmp_path / QUEUE_FILE
    queue_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(queue_store.QueueStoreCorruptError):
        queue_persistence.load_entries(tmp_path)

    queue_path.write_text(json.dumps({"status": "bad"}), encoding="utf-8")
    with pytest.raises(queue_store.QueueStoreCorruptError):
        queue_persistence.load_entries(tmp_path)

    queue_path.write_text(
        json.dumps(
            [
                {
                    "queue_id": "q_ok",
                    "app_name": "orca_auto_orca",
                    "task_id": "task_ok",
                    "task_kind": "orca_run_inp",
                    "engine": "orca",
                    "status": "pending",
                    "priority": 10,
                    "enqueued_at": "2026-03-10T00:00:00+00:00",
                    "started_at": "",
                    "finished_at": "",
                    "cancel_requested": False,
                    "error": "",
                    "metadata": {},
                },
                "bad",
                [],
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(queue_store.QueueStoreCorruptError, match="must be a JSON object"):
        queue_persistence.load_entries(tmp_path)


def test_enqueue_overwrites_worker_log_metadata_with_safe_queue_log(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()

    entry = queue_adapter.enqueue(
        tmp_path,
        str(reaction_dir),
        metadata={"worker_log": "/tmp/unsafe-worker.log"},
    )

    metadata = queue_entries.queue_entry_metadata(entry)
    assert metadata["worker_log"] == str((tmp_path / "logs" / f"{entry.queue_id}.log").resolve())


def test_apply_terminal_reconciliation_updates_fields_and_clears_completed_error() -> None:
    completed_entry = _entry(
        "q_done",
        "/tmp/rxn",
        QueueStatus.RUNNING.value,
        finished_at=None,
        error="stale_error",
    )
    with patch(
        "orca_auto.core.queue.transitions.now_utc_iso",
        return_value="2026-03-10T06:00:00+00:00",
    ):
        completed_entry = queue_orphans.apply_terminal_reconciliation(
            completed_entry,
            status=QueueStatus.COMPLETED.value,
            run_id="run_done",
            finished_at=None,
        )

    assert completed_entry.status == QueueStatus.COMPLETED
    assert completed_entry.finished_at == "2026-03-10T06:00:00+00:00"
    assert queue_entries.queue_entry_run_id(completed_entry) == "run_done"
    assert completed_entry.error == ""

    failed_entry = _entry(
        "q_fail",
        "/tmp/rxn",
        QueueStatus.RUNNING.value,
        finished_at="2026-03-10T01:00:00+00:00",
    )
    failed_entry = queue_orphans.apply_terminal_reconciliation(
        failed_entry,
        status=QueueStatus.FAILED.value,
        run_id=None,
        finished_at=None,
        error="boom",
    )
    assert failed_entry.finished_at == "2026-03-10T01:00:00+00:00"
    assert failed_entry.error == "boom"


def test_find_active_entry_matches_first_active_for_reaction_dir() -> None:
    entries = [
        _entry("q_pending", "/tmp/a", QueueStatus.PENDING.value),
        _entry("q_running", "/tmp/a", QueueStatus.RUNNING.value),
        _entry("q_done", "/tmp/a", QueueStatus.COMPLETED.value),
        _entry("q_cancelled", "/tmp/b", QueueStatus.CANCELLED.value),
    ]

    assert queue_entries.find_active_entry(entries, "/tmp/a") == entries[0]
    assert queue_entries.find_active_entry(entries, "/tmp/missing") is None


def test_orca_queue_view_and_mutations_ignore_foreign_rows(tmp_path: Path) -> None:
    reaction_dir = str(tmp_path / "rxn")
    foreign_pending = _foreign_entry("q_foreign_pending", reaction_dir=reaction_dir)
    foreign_terminal = _foreign_entry(
        "q_foreign_terminal",
        status=QueueStatus.COMPLETED,
        reaction_dir=reaction_dir,
    )
    _save_entries(tmp_path, [foreign_pending, foreign_terminal])

    assert queue_adapter.list_queue(tmp_path) == []
    assert queue_adapter.get_active_entry_for_reaction_dir(tmp_path, reaction_dir) is None
    assert queue_adapter.cancel(tmp_path, foreign_pending.queue_id) is None
    assert run_cleanup.clear_terminal_queue_entries(tmp_path) == (0, 0)

    durable = queue_persistence.load_entries(tmp_path)
    assert [(entry.queue_id, entry.status) for entry in durable] == [
        (foreign_pending.queue_id, QueueStatus.PENDING),
        (foreign_terminal.queue_id, QueueStatus.COMPLETED),
    ]

    created = queue_adapter.enqueue(tmp_path, reaction_dir)
    assert created.engine == "orca"


def test_queue_entry_accessors_read_common_fields_from_metadata(tmp_path: Path) -> None:
    entry = queue_persistence.entry_from_dict(
        {
            "queue_id": "q_meta",
            "app_name": "orca_auto_orca",
            "task_id": "task_meta",
            "task_kind": "orca_run_inp",
            "engine": "orca",
            "status": "PENDING",
            "priority": 7,
            "enqueued_at": "2026-03-10T00:00:00+00:00",
            "started_at": "",
            "finished_at": "",
            "cancel_requested": False,
            "error": "",
            "metadata": {
                "reaction_dir": str(tmp_path / "rxn"),
                "force": True,
            },
        }
    )

    assert queue_entries.queue_entry_id(entry) == "q_meta"
    assert queue_entries.queue_entry_task_id(entry) == "task_meta"
    assert queue_entries.queue_entry_status(entry) == QueueStatus.PENDING.value
    assert queue_entries.queue_entry_priority(entry) == 7
    assert queue_entries.queue_entry_force(entry) is True
    assert queue_entries.queue_entry_app_name(entry) == "orca_auto_orca"
    assert queue_entries.queue_entry_reaction_dir(entry) == str(tmp_path / "rxn")
    assert queue_entries.queue_entry_metadata(entry)["reaction_dir"] == str(tmp_path / "rxn")


def test_save_entries_uses_core_queue_entry_as_storage_model(tmp_path: Path) -> None:
    root = tmp_path / "queue_root"
    root.mkdir()

    _save_entries(
        root,
        [
            queue_persistence.entry_from_dict(
                {
                    "queue_id": "q_backend",
                    "app_name": "orca_auto_orca",
                    "task_id": "task_backend",
                    "task_kind": "orca_run_inp",
                    "engine": "orca",
                    "status": QueueStatus.RUNNING.value,
                    "priority": 10,
                    "enqueued_at": "2026-03-10T00:00:00+00:00",
                    "started_at": "2026-03-10T00:01:00+00:00",
                    "finished_at": "",
                    "cancel_requested": False,
                    "error": "",
                    "metadata": {
                        "reaction_dir": str(root / "rxn"),
                        "force": True,
                        "run_id": "run_backend",
                    },
                }
            )
        ],
    )

    payload = json.loads((root / QUEUE_FILE).read_text(encoding="utf-8"))
    assert payload[0]["app_name"] == "orca_auto_orca"
    assert payload[0]["task_id"] == "task_backend"
    assert payload[0]["task_kind"] == "orca_run_inp"
    assert payload[0]["engine"] == "orca"
    assert payload[0]["status"] == QueueStatus.RUNNING.value
    assert payload[0]["metadata"] == {
        "reaction_dir": str(root / "rxn"),
        "force": True,
        "run_id": "run_backend",
    }
    assert "reaction_dir" not in payload[0]
    assert "force" not in payload[0]
    assert "run_id" not in payload[0]


def test_reconcile_orphaned_running_entries_covers_state_terminal_paths_and_pending_fallback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "queue_root"
    root.mkdir()
    completed_dir = root / "completed"
    failed_dir = root / "failed"
    pending_dir = root / "pending"
    for path in (completed_dir, failed_dir, pending_dir):
        path.mkdir()

    _save_entries(
        root,
        [
            _entry(
                "q_done",
                str(completed_dir),
                QueueStatus.RUNNING.value,
                started_at="2026-03-10T00:10:00+00:00",
            ),
            _entry(
                "q_fail",
                str(failed_dir),
                QueueStatus.RUNNING.value,
                started_at="2026-03-10T00:20:00+00:00",
            ),
            _entry(
                "q_requeue",
                str(pending_dir),
                QueueStatus.RUNNING.value,
                started_at="2026-03-10T00:30:00+00:00",
            ),
        ],
    )

    def _load_state(reaction_dir: Path):
        if reaction_dir == completed_dir:
            return {
                "job_id": "q_done",
                "run_id": "run_done",
                "status": RunStatus.COMPLETED.value,
                "updated_at": "2026-03-10T02:00:00+00:00",
                "final_result": {"completed_at": "2026-03-10T01:59:00+00:00"},
            }
        if reaction_dir == failed_dir:
            return {
                "job_id": "q_fail",
                "run_id": "run_fail",
                "status": RunStatus.FAILED.value,
                "updated_at": "2026-03-10T03:00:00+00:00",
                "final_result": {
                    "completed_at": "2026-03-10T02:59:00+00:00",
                    "reason": "orca_crash",
                },
            }
        return None

    with (
        patch("orca_auto.orca.queue.orphans.read_worker_pid_file", return_value=None),
        patch(
            "orca_auto.orca.queue.orphans.run_lock_is_held",
            return_value=False,
        ),
        patch(
            "orca_auto.orca.queue.orphans.load_state",
            side_effect=_load_state,
        ),
    ):
        changed = queue_orphans.reconcile_orphaned_running_entries(root)

    assert changed == 3
    entries = {entry.queue_id: entry for entry in queue_adapter.list_queue(root)}
    assert entries["q_done"].status == QueueStatus.COMPLETED
    assert queue_entries.queue_entry_run_id(entries["q_done"]) == "run_done"
    _assert_terminal_replay_marker(
        entries["q_done"],
        status=QueueStatus.COMPLETED,
        error="",
    )
    assert entries["q_fail"].status == QueueStatus.FAILED
    assert entries["q_fail"].error == "orca_crash"
    _assert_terminal_replay_marker(
        entries["q_fail"],
        status=QueueStatus.FAILED,
        error="orca_crash",
    )
    assert entries["q_requeue"].status == QueueStatus.PENDING
    assert entries["q_requeue"].started_at == ""


def test_reconcile_orphaned_running_entries_skips_blank_dirs_and_active_locks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "queue_root"
    root.mkdir()
    locked_dir = root / "locked"
    locked_dir.mkdir()

    _save_entries(
        root,
        [
            _entry("q_blank", "", QueueStatus.RUNNING.value),
            _entry("q_locked", str(locked_dir), QueueStatus.RUNNING.value),
        ],
    )

    with (
        patch("orca_auto.orca.queue.orphans.read_worker_pid_file", return_value=None),
        patch(
            "orca_auto.orca.queue.orphans.run_lock_is_held",
            side_effect=lambda reaction_dir, **_kwargs: reaction_dir == locked_dir,
        ),
    ):
        changed = queue_orphans.reconcile_orphaned_running_entries(root)

    assert changed == 0
    entries = {entry.queue_id: entry for entry in queue_adapter.list_queue(root)}
    assert entries["q_blank"].status == QueueStatus.RUNNING
    assert entries["q_locked"].status == QueueStatus.RUNNING


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (QueueStatus.COMPLETED, ""),
        (QueueStatus.FAILED, "exit_code=1"),
        (QueueStatus.CANCELLED, "cancel_requested"),
    ],
)
def test_orca_terminal_marks_persist_valid_replay_marker(
    tmp_path: Path,
    status: QueueStatus,
    error: str,
) -> None:
    root = tmp_path / "queue_root"
    reaction_dir = root / status.value
    reaction_dir.mkdir(parents=True)
    entry = queue_adapter.enqueue(root, str(reaction_dir), task_id=f"task-{status.value}")
    running = claim_next_entry(root)
    assert running is not None

    if status == QueueStatus.COMPLETED:
        changed = queue_adapter.mark_completed(root, entry.queue_id, expected_entry=running)
    elif status == QueueStatus.FAILED:
        changed = queue_adapter.mark_failed(
            root,
            entry.queue_id,
            error=error,
            expected_entry=running,
        )
    else:
        changed = queue_adapter.mark_cancelled(root, entry.queue_id, expected_entry=running)

    assert changed is True
    [terminal] = queue_adapter.list_queue(root)
    assert terminal.status == status
    if status == QueueStatus.CANCELLED:
        assert terminal.error == ""
    _assert_terminal_replay_marker(terminal, status=status, error=error)


def test_orca_pending_cancel_persists_valid_replay_marker(tmp_path: Path) -> None:
    root = tmp_path / "queue_root"
    reaction_dir = root / "pending_cancel"
    reaction_dir.mkdir(parents=True)
    entry = queue_adapter.enqueue(root, str(reaction_dir), task_id="task-pending-cancel")

    cancelled = queue_adapter.cancel(root, entry.queue_id, expected_entry=entry)

    assert cancelled is not None
    assert cancelled.status == QueueStatus.CANCELLED
    [persisted] = queue_adapter.list_queue(root)
    assert persisted == cancelled
    _assert_terminal_replay_marker(
        persisted,
        status=QueueStatus.CANCELLED,
        error="cancel_requested",
    )


def test_orca_requeue_honors_racing_cancel_with_valid_replay_marker(tmp_path: Path) -> None:
    root = tmp_path / "queue_root"
    reaction_dir = root / "requeue_cancel"
    reaction_dir.mkdir(parents=True)
    entry = queue_adapter.enqueue(root, str(reaction_dir), task_id="task-requeue-cancel")
    running = claim_next_entry(root)
    assert running is not None
    cancel_requested = queue_adapter.cancel(root, entry.queue_id, expected_entry=running)
    assert cancel_requested is not None
    assert cancel_requested.status == QueueStatus.RUNNING
    assert cancel_requested.cancel_requested is True

    assert (
        queue_adapter.requeue_running_entry(
            root,
            entry.queue_id,
            expected_entry=cancel_requested,
        )
        is True
    )

    [cancelled] = queue_adapter.list_queue(root)
    assert cancelled.status == QueueStatus.CANCELLED
    assert cancelled.cancel_requested is False
    _assert_terminal_replay_marker(
        cancelled,
        status=QueueStatus.CANCELLED,
        error="cancel_requested",
    )


def test_same_terminal_mark_after_replay_clear_does_not_resurrect_marker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "queue_root"
    reaction_dir = root / "completed"
    reaction_dir.mkdir(parents=True)
    entry = queue_adapter.enqueue(root, str(reaction_dir), task_id="task-completed")
    running = claim_next_entry(root)
    assert running is not None
    assert queue_adapter.mark_completed(root, entry.queue_id, expected_entry=running) is True
    [terminal] = queue_adapter.list_queue(root)
    _assert_terminal_replay_marker(terminal, status=QueueStatus.COMPLETED, error="")

    assert queue_adapter.update_metadata(
        root,
        entry.queue_id,
        {TERMINAL_REPLAY_METADATA_KEY: None},
        expected_entry=terminal,
    )
    [closed] = queue_adapter.list_queue(root)
    assert closed.metadata[TERMINAL_REPLAY_METADATA_KEY] is None

    assert queue_adapter.mark_completed(root, entry.queue_id, expected_entry=closed) is True
    [stable] = queue_adapter.list_queue(root)
    assert stable == closed
    assert stable.metadata[TERMINAL_REPLAY_METADATA_KEY] is None


def test_administrative_failed_mark_rejects_side_effect_marker(tmp_path: Path) -> None:
    root = tmp_path / "queue_root"
    reaction_dir = root / "administrative_fence"
    reaction_dir.mkdir(parents=True)
    entry = queue_adapter.enqueue(root, str(reaction_dir), task_id="task-fence")
    before = (root / QUEUE_FILE).read_bytes()

    with pytest.raises(ValueError, match="cannot carry a side-effect replay marker"):
        queue_adapter.mark_failed(
            root,
            entry.queue_id,
            error="administrative_fence",
            publish_terminal_side_effects=False,
            metadata_update={TERMINAL_REPLAY_METADATA_KEY: {"version": 1}},
            expected_entry=entry,
        )

    assert (root / QUEUE_FILE).read_bytes() == before
    [unchanged] = queue_adapter.list_queue(root)
    assert unchanged.status == QueueStatus.PENDING

    running = claim_next_entry(root)
    assert running is not None
    assert queue_adapter.mark_failed(
        root,
        entry.queue_id,
        error="worker_start_error",
        expected_entry=running,
    )
    [pending_replay] = queue_adapter.list_queue(root)
    with pytest.raises(ValueError, match="cannot replace pending side-effect replay"):
        queue_adapter.mark_failed(
            root,
            entry.queue_id,
            error="administrative_fence",
            publish_terminal_side_effects=False,
            expected_entry=pending_replay,
        )
    assert queue_adapter.list_queue(root) == [pending_replay]


@pytest.mark.parametrize("marker_kind", ["malformed", "unsupported"])
def test_invalid_terminal_replay_marker_blocks_clear_and_forced_successor(
    tmp_path: Path,
    marker_kind: str,
) -> None:
    root = tmp_path / "queue_root"
    reaction_dir = root / marker_kind
    reaction_dir.mkdir(parents=True)
    entry = queue_adapter.enqueue(root, str(reaction_dir), task_id=f"task-{marker_kind}")
    running = claim_next_entry(root)
    assert running is not None
    assert queue_adapter.mark_completed(root, entry.queue_id, expected_entry=running) is True

    marker: dict[str, Any]
    if marker_kind == "malformed":
        marker = {"version": 1, "task_id": entry.task_id}
    else:
        marker = {
            "version": 2,
            "task_id": entry.task_id,
            "selected_inp": "",
            "status": QueueStatus.COMPLETED.value,
            "error": "",
            "observed_state": {
                "present": False,
                "readable": True,
                "job_id": "",
                "run_id": "",
                "terminal_status": "",
            },
        }
    assert queue_adapter.update_metadata(
        root,
        entry.queue_id,
        {TERMINAL_REPLAY_METADATA_KEY: marker},
    )

    [blocked] = queue_adapter.list_queue(root)
    assert terminal_replay_marker_from_entry(blocked) is None
    assert run_cleanup.clear_terminal_queue_entries(root) == (0, 0)
    with pytest.raises(queue_adapter.DuplicateEntryError):
        queue_adapter.enqueue(root, str(reaction_dir), force=True)


def test_mark_cancelled_requeue_cancel_and_update_terminal_cover_missing_and_wrong_statuses(
    tmp_path: Path,
) -> None:
    root = tmp_path / "queue_root"
    root.mkdir()
    _save_entries(
        root,
        [
            _entry("q_pending", str(root / "pending"), QueueStatus.PENDING.value),
            _entry("q_running", str(root / "running"), QueueStatus.RUNNING.value),
            _entry("q_terminal", str(root / "terminal"), QueueStatus.COMPLETED.value),
        ],
    )

    assert queue_adapter.mark_cancelled(root, "q_missing") is False
    assert queue_adapter.mark_cancelled(root, "q_pending") is False
    assert queue_adapter.mark_cancelled(root, "q_running") is True

    entries = {entry.queue_id: entry for entry in queue_adapter.list_queue(root)}
    assert entries["q_running"].status == QueueStatus.CANCELLED
    assert entries["q_running"].cancel_requested is False

    _save_entries(
        root,
        [
            _entry("q_running", str(root / "running"), QueueStatus.RUNNING.value),
            _entry("q_terminal", str(root / "terminal"), QueueStatus.COMPLETED.value),
        ],
    )
    assert queue_adapter.requeue_running_entry(root, "q_missing") is False
    assert queue_adapter.requeue_running_entry(root, "q_terminal") is False
    assert queue_adapter.requeue_running_entry(root, "q_running") is True

    entries = {entry.queue_id: entry for entry in queue_adapter.list_queue(root)}
    assert entries["q_running"].status == QueueStatus.PENDING
    assert entries["q_running"].started_at == ""
    assert entries["q_running"].cancel_requested is False

    # A cancel requested mid-run must not be undone by the shutdown requeue path:
    # the entry is cancelled (terminal), not returned to pending for a resume.
    _save_entries(
        root,
        [
            _entry(
                "q_running", str(root / "running"), QueueStatus.RUNNING.value, cancel_requested=True
            ),
        ],
    )
    assert queue_adapter.requeue_running_entry(root, "q_running") is True
    entries = {entry.queue_id: entry for entry in queue_adapter.list_queue(root)}
    assert entries["q_running"].status == QueueStatus.CANCELLED
    assert entries["q_running"].cancel_requested is False

    _save_entries(
        root,
        [
            _entry("q_pending", str(root / "pending"), QueueStatus.PENDING.value),
            _entry("q_running", str(root / "running"), QueueStatus.RUNNING.value),
            _entry("q_terminal", str(root / "terminal"), QueueStatus.COMPLETED.value),
        ],
    )
    assert queue_adapter.cancel(root, "q_missing") is None
    assert queue_adapter.cancel(root, "q_terminal") is None
    assert queue_adapter.cancel(root, "q_pending") is not None
    running_entry = queue_adapter.cancel(root, "q_running")
    assert running_entry is not None
    assert running_entry.cancel_requested is True
    assert queue_adapter.get_cancel_requested(root, "q_running") is True
    assert queue_adapter.get_cancel_requested(root, "q_missing") is False

    assert queue_adapter.update_terminal(root, "q_missing", QueueStatus.COMPLETED.value) is False
    assert queue_adapter.update_terminal(root, "q_terminal", QueueStatus.RUNNING.value) is False


def test_orca_adapter_mutations_never_change_foreign_engine_rows(tmp_path: Path) -> None:
    root = tmp_path / "shared_queue"
    root.mkdir()
    pending = _foreign_entry("foreign-pending")
    running = _foreign_entry("foreign-running", status=QueueStatus.RUNNING)
    terminal = _foreign_entry("foreign-terminal", status=QueueStatus.COMPLETED)
    _save_entries(root, [pending, running, terminal])

    assert claim_next_entry(root) is None
    assert queue_adapter.dequeue_entry_if_pending(root, pending.queue_id) is None
    assert queue_adapter.mark_completed(root, pending.queue_id) is False
    assert queue_adapter.mark_failed(root, pending.queue_id, error="foreign") is False
    assert queue_adapter.cancel(root, pending.queue_id) is None
    assert queue_adapter.requeue_running_entry(root, running.queue_id) is False
    assert queue_adapter.mark_cancelled(root, running.queue_id) is False
    assert queue_adapter.update_metadata(root, running.queue_id, {"foreign": "changed"}) is False
    assert queue_adapter.get_cancel_requested(root, running.queue_id) is False
    assert (
        queue_adapter.update_terminal(
            root,
            terminal.queue_id,
            QueueStatus.FAILED.value,
        )
        is False
    )

    assert queue_store.list_queue(root) == [pending, running, terminal]


def test_orca_adapter_expected_generation_rejects_replaced_queue_id(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    stale = _entry("same-id", str(root / "old"), QueueStatus.RUNNING.value)
    replacement = _entry("same-id", str(root / "new"), QueueStatus.RUNNING.value)
    replacement = QueueEntry(
        **{
            **replacement.__dict__,
            "task_id": "replacement-task",
        }
    )
    _save_entries(root, [replacement])

    assert queue_adapter.mark_completed(root, stale.queue_id, expected_entry=stale) is False
    assert (
        queue_adapter.mark_failed(
            root,
            stale.queue_id,
            error="stale",
            expected_entry=stale,
        )
        is False
    )
    assert (
        queue_adapter.requeue_running_entry(
            root,
            stale.queue_id,
            expected_entry=stale,
        )
        is False
    )
    assert queue_adapter.cancel(root, stale.queue_id, expected_entry=stale) is None
    assert (
        queue_adapter.update_terminal(
            root,
            stale.queue_id,
            QueueStatus.FAILED.value,
            expected_entry=stale,
        )
        is False
    )
    assert update_metadata(root, stale.queue_id, {"stale": True}, expected_entry=stale) is False
    assert get_cancel_requested(root, stale.queue_id, expected_entry=stale) is False

    [current] = queue_adapter.list_queue(root)
    assert current == replacement


def test_orca_dequeue_rejects_a_replaced_queue_id(tmp_path: Path) -> None:
    stale = _entry("same-id", str(tmp_path / "old"), QueueStatus.PENDING.value)
    replacement = replace(stale, task_id="replacement-task")
    _save_entries(tmp_path, [replacement])

    assert queue_adapter.dequeue_entry_if_pending(tmp_path, "same-id", expected_entry=stale) is None
    assert queue_adapter.list_queue(tmp_path) == [replacement]


def test_orca_adapter_expected_task_rejects_another_task(tmp_path: Path) -> None:
    failed = _entry("q-failed", str(tmp_path / "job"), QueueStatus.FAILED.value)
    _save_entries(tmp_path, [failed])

    assert (
        queue_adapter.update_terminal(
            tmp_path, failed.queue_id, QueueStatus.COMPLETED.value, expected_task_id="other-task"
        )
        is False
    )
    assert queue_adapter.list_queue(tmp_path) == [failed]


def test_orca_adapter_fence_ignores_publication_lease_changes(tmp_path: Path) -> None:
    job = tmp_path / "job"
    selected = replace(
        _entry("q-lease", str(job), QueueStatus.PENDING.value),
        metadata={
            "reaction_dir": str(job),
            "force": False,
            **queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_PREPARING, token="publication-token", owner_pid=os.getpid()
            ),
        },
    )
    published = replace(
        selected,
        metadata={
            **selected.metadata,
            **queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_COMPLETE, token="publication-token", owner_pid=0
            ),
        },
    )
    _save_entries(tmp_path, [published])

    cancelled = cancel(tmp_path, selected.queue_id, expected_entry=selected)

    assert cancelled is not None
    assert cancelled.status == QueueStatus.CANCELLED


def test_orca_update_metadata_refuses_a_writer_behind_an_identity_change(tmp_path: Path) -> None:
    submitted = _entry("q-meta", str(tmp_path / "job"), QueueStatus.PENDING.value)
    _save_entries(tmp_path, [submitted])

    assert update_metadata(
        tmp_path, submitted.queue_id, {"attached": True}, expected_entry=submitted
    )
    assert (
        update_metadata(tmp_path, submitted.queue_id, {"stale": True}, expected_entry=submitted)
        is False
    )
    [current] = queue_adapter.list_queue(tmp_path)
    assert current.metadata["attached"] is True
    assert "stale" not in current.metadata
