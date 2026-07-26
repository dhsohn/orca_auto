from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    queue_record_sync_metadata,
)
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.queue import adapter as queue_adapter
from orca_auto.orca.queue import entries as queue_entries
from orca_auto.orca.queue import orphans as queue_orphans
from orca_auto.orca.queue.terminal_replay import (
    TERMINAL_REPLAY_METADATA_KEY,
    terminal_replay_marker_from_entry,
)
from orca_auto.orca.statuses import RunStatus


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
    return queue_entries.entry_from_json_payload(entry)


def _load_entries(root: Path) -> list[QueueEntry]:
    return queue_store.load_entries(
        root,
        entry_from_dict_fn=queue_entries.entry_from_json_payload,
        corrupt_error=queue_store.QueueStoreCorruptError,
    )


def _save_entries(root: Path, entries: list[QueueEntry]) -> None:
    queue_store.save_entries(root, entries)


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
        app_name="orca_auto_xtb",
        task_id=f"xtb-{queue_id}",
        task_kind="xtb_sp",
        engine="xtb",
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
    assert _load_entries(tmp_path) == []

    queue_path = tmp_path / queue_entries.QUEUE_FILE_NAME
    queue_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(queue_store.QueueStoreCorruptError):
        _load_entries(tmp_path)

    queue_path.write_text(json.dumps({"status": "bad"}), encoding="utf-8")
    with pytest.raises(queue_store.QueueStoreCorruptError):
        _load_entries(tmp_path)

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
        _load_entries(tmp_path)


def test_enqueue_overwrites_worker_log_metadata_with_safe_queue_log(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()

    entry = queue_adapter.enqueue(
        tmp_path,
        str(reaction_dir),
        metadata={"worker_log": "/tmp/unsafe-worker.log"},
    )

    metadata = queue_adapter.queue_entry_metadata(entry)
    assert metadata["worker_log"] == str((tmp_path / "logs" / f"{entry.queue_id}.log").resolve())


def test_apply_terminal_reconciliation_updates_fields_and_clears_completed_error() -> None:
    completed_entry = _entry(
        "q_done",
        "/tmp/rxn",
        QueueStatus.RUNNING.value,
        finished_at=None,
        error="stale_error",
    )
    with patch("orca_auto.orca.queue.orphans._now_iso", return_value="2026-03-10T06:00:00+00:00"):
        completed_entry = queue_orphans.apply_terminal_reconciliation(
            completed_entry,
            status=QueueStatus.COMPLETED.value,
            run_id="run_done",
            finished_at=None,
        )

    assert completed_entry.status == QueueStatus.COMPLETED
    assert completed_entry.finished_at == "2026-03-10T06:00:00+00:00"
    assert queue_adapter.queue_entry_run_id(completed_entry) == "run_done"
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


def test_find_entry_by_target_matches_orca_cancel_aliases(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    entry = _entry(
        "q_running",
        str(reaction_dir),
        QueueStatus.RUNNING.value,
        run_id="run_done",
    )

    entries = [entry]

    assert queue_adapter.find_entry_by_target(entries, "q_running") == entry
    assert queue_adapter.find_entry_by_target(entries, "run_done") == entry
    assert queue_adapter.find_entry_by_target(entries, str(reaction_dir)) == entry
    assert queue_adapter.find_entry_by_target(entries, str(reaction_dir.resolve())) == entry
    assert queue_adapter.find_entry_by_target(entries, "missing") is None


def test_find_entry_by_target_prefers_active_orca_generation_and_rejects_ambiguity(
    tmp_path: Path,
) -> None:
    reaction_dir = str(tmp_path / "rxn")
    old = _entry(
        "q_old",
        reaction_dir,
        QueueStatus.COMPLETED.value,
        finished_at="2026-03-10T00:01:00+00:00",
    )
    active = _entry("q_active", reaction_dir, QueueStatus.PENDING.value)

    assert queue_adapter.find_entry_by_target([old, active], reaction_dir) == active
    assert (
        queue_adapter.find_entry_by_target(
            [old, _foreign_entry("q_foreign", reaction_dir=reaction_dir)],
            reaction_dir,
        )
        == old
    )

    second_active = _entry("q_active_2", reaction_dir, QueueStatus.RUNNING.value)
    with pytest.raises(queue_adapter.AmbiguousQueueTargetError, match="multiple active"):
        queue_adapter.find_entry_by_target([active, second_active], reaction_dir)


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
    assert queue_adapter.clear_terminal(tmp_path) == 0

    durable = _load_entries(tmp_path)
    assert [(entry.queue_id, entry.status) for entry in durable] == [
        (foreign_pending.queue_id, QueueStatus.PENDING),
        (foreign_terminal.queue_id, QueueStatus.COMPLETED),
    ]

    created = queue_adapter.enqueue(tmp_path, reaction_dir)
    assert created.engine == "orca"


def test_queue_entry_accessors_read_common_fields_from_metadata(tmp_path: Path) -> None:
    entry = queue_entries.entry_from_json_payload(
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

    assert queue_adapter.queue_entry_id(entry) == "q_meta"
    assert queue_adapter.queue_entry_task_id(entry) == "task_meta"
    assert queue_adapter.queue_entry_status(entry) == QueueStatus.PENDING.value
    assert queue_adapter.queue_entry_priority(entry) == 7
    assert queue_adapter.queue_entry_force(entry) is True
    assert queue_adapter.queue_entry_app_name(entry) == "orca_auto_orca"
    assert queue_adapter.queue_entry_reaction_dir(entry) == str(tmp_path / "rxn")
    assert queue_adapter.queue_entry_metadata(entry)["reaction_dir"] == str(tmp_path / "rxn")


def test_save_entries_uses_core_queue_entry_as_storage_model(tmp_path: Path) -> None:
    root = tmp_path / "queue_root"
    root.mkdir()

    _save_entries(
        root,
        [
            queue_entries.entry_from_json_payload(
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

    payload = json.loads((root / queue_entries.QUEUE_FILE_NAME).read_text(encoding="utf-8"))
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
        patch("orca_auto.orca.queue.orphans.read_worker_pid", return_value=None),
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
    assert queue_adapter.queue_entry_run_id(entries["q_done"]) == "run_done"
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
        patch("orca_auto.orca.queue.orphans.read_worker_pid", return_value=None),
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
    running = queue_adapter.dequeue_next(root)
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
    running = queue_adapter.dequeue_next(root)
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
    running = queue_adapter.dequeue_next(root)
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
    before = (root / queue_entries.QUEUE_FILE_NAME).read_bytes()

    with pytest.raises(ValueError, match="cannot carry a side-effect replay marker"):
        queue_adapter.mark_failed(
            root,
            entry.queue_id,
            error="administrative_fence",
            publish_terminal_side_effects=False,
            metadata_update={TERMINAL_REPLAY_METADATA_KEY: {"version": 1}},
            expected_entry=entry,
        )

    assert (root / queue_entries.QUEUE_FILE_NAME).read_bytes() == before
    [unchanged] = queue_adapter.list_queue(root)
    assert unchanged.status == QueueStatus.PENDING

    running = queue_adapter.dequeue_next(root)
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
    running = queue_adapter.dequeue_next(root)
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
    assert queue_adapter.clear_terminal(root) == 0
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

    assert queue_adapter.dequeue_next(root) is None
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


def _driver_recovery_spec(root: Path) -> Any:
    from orca_auto.core.queue.enqueue_publication import EnqueuePublicationSpec

    return EnqueuePublicationSpec(
        queue_root=root,
        app_name="orca_auto_orca",
        task_id="task-ambiguous",
        task_kind="orca_run_inp",
        engine="orca",
        priority=7,
        metadata={},
        label="ORCA",
        publish=lambda _entry: None,
        job_dir_metadata_key="reaction_dir",
        ambiguous_fence_metadata={queue_adapter.TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY: True},
    )


def test_enqueue_recovery_never_matches_a_foreign_row(tmp_path: Path) -> None:
    from orca_auto.core.queue.enqueue_publication import _recover_committed_enqueue

    root = tmp_path / "shared_queue"
    reaction_dir = str((root / "reaction").resolve())
    token = "publication-token"
    metadata = {
        "reaction_dir": reaction_dir,
        "force": False,
        "_orca_auto_queued_record_sync": "preparing",
        "_orca_auto_queued_record_sync_token": token,
        "_orca_auto_queued_record_sync_owner_pid": 1234,
        "_orca_auto_queued_record_sync_owner_start": "owner-start",
    }
    foreign = QueueEntry(
        queue_id="queue-foreign",
        app_name="orca_auto_orca",
        task_id="task-ambiguous",
        task_kind="xtb_sp",
        engine="orca",
        priority=7,
        metadata=dict(metadata),
    )
    _save_entries(root, [foreign])
    queue_path = root / queue_entries.QUEUE_FILE_NAME
    before = queue_path.read_bytes()

    spec = _driver_recovery_spec(root)
    recovered = _recover_committed_enqueue(spec, enqueue_metadata=dict(metadata))

    # The row differs in task_kind: the strict identity match refuses it even
    # though it carries this attempt's publication token, and nothing mutates.
    assert recovered is None
    assert queue_path.read_bytes() == before


def test_ambiguous_enqueue_recovery_is_durable_fence_only_history(
    tmp_path: Path,
) -> None:
    from orca_auto.core.queue.enqueue_publication import (
        EnqueuePublicationOutcomeUnknown,
        _recover_committed_enqueue,
    )

    root = tmp_path / "shared_queue"
    reaction_dir = root / "reaction"
    reaction_dir.mkdir(parents=True)
    token = "ambiguous-publication-token"
    metadata = {
        "reaction_dir": str(reaction_dir.resolve()),
        "force": False,
        "_orca_auto_queued_record_sync": "preparing",
        "_orca_auto_queued_record_sync_token": token,
        "_orca_auto_queued_record_sync_owner_pid": 1234,
        "_orca_auto_queued_record_sync_owner_start": "owner-start",
    }
    entries = [
        QueueEntry(
            queue_id=queue_id,
            app_name="orca_auto_orca",
            task_id="task-ambiguous",
            task_kind="orca_run_inp",
            engine="orca",
            priority=7,
            metadata=dict(metadata),
        )
        for queue_id in ("queue-a", "queue-b")
    ]
    _save_entries(root, entries)

    spec = _driver_recovery_spec(root)
    with pytest.raises(EnqueuePublicationOutcomeUnknown):
        _recover_committed_enqueue(spec, enqueue_metadata=dict(metadata))

    fenced = queue_adapter.list_queue(root)
    assert len(fenced) == 2
    assert {entry.status for entry in fenced} == {QueueStatus.CANCELLED}
    # The administrative fence-only marker keeps a successor generation for
    # the same reaction_dir blocked until the ambiguous rows are cleared.
    assert all(
        entry.metadata.get(queue_adapter.TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY) is True
        for entry in fenced
    )


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

    [current] = queue_adapter.list_queue(root)
    assert current == replacement


def test_clear_terminal_keep_last_keeps_newest_terminal_entries(tmp_path: Path) -> None:
    root = tmp_path / "queue_root"
    root.mkdir()
    _save_entries(
        root,
        [
            _entry("q_pending", str(root / "pending"), QueueStatus.PENDING.value),
            _entry(
                "q_old",
                str(root / "old"),
                QueueStatus.COMPLETED.value,
                finished_at="2026-03-10T01:00:00+00:00",
            ),
            _entry(
                "q_new",
                str(root / "new"),
                QueueStatus.FAILED.value,
                finished_at="2026-03-10T03:00:00+00:00",
            ),
            _entry(
                "q_mid",
                str(root / "mid"),
                QueueStatus.CANCELLED.value,
                finished_at="2026-03-10T02:00:00+00:00",
            ),
        ],
    )

    removed = queue_adapter.clear_terminal(root, keep_last=2)

    assert removed == 1
    remaining = {entry.queue_id: entry for entry in queue_adapter.list_queue(root)}
    assert set(remaining) == {"q_pending", "q_new", "q_mid"}
