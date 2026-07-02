from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.queue import adapter as queue_adapter
from orca_auto.orca.queue import entries as queue_entries
from orca_auto.orca.queue import orphans as queue_orphans
from orca_auto.orca.state import report_json_path
from orca_auto.orca.statuses import RunStatus
from tests.engine_artifact_helpers import orca_artifact_payload


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
        "status": status,
        "priority": priority,
        "enqueued_at": "2026-03-10T00:00:00+00:00",
        "started_at": started_at,
        "finished_at": finished_at,
        "cancel_requested": cancel_requested,
        "error": error,
        "metadata": {
            "reaction_dir": reaction_dir,
            "force": False,
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
        json.dumps([{"queue_id": "q_ok", "status": "pending"}, "bad", []]),
        encoding="utf-8",
    )
    with pytest.raises(queue_store.QueueStoreCorruptError, match="must be a JSON object"):
        _load_entries(tmp_path)

    queue_path.write_text(
        json.dumps([{"queue_id": "q_ok", "status": "pending"}]),
        encoding="utf-8",
    )
    [entry] = _load_entries(tmp_path)
    assert entry.queue_id == "q_ok"
    assert entry.status == QueueStatus.PENDING
    assert entry.app_name == "orca_auto_orca"
    assert entry.task_id == "q_ok"


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


def test_report_payload_and_terminal_report_data_cover_missing_invalid_completed_and_failed(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()

    assert queue_orphans.load_report_payload(reaction_dir) is None
    assert queue_orphans.terminal_report_data(reaction_dir) is None

    report_path = report_json_path(reaction_dir)
    report_path.write_text("{not-json", encoding="utf-8")
    assert queue_orphans.load_report_payload(reaction_dir) is None

    report_path.write_text(json.dumps(["bad"]), encoding="utf-8")
    assert queue_orphans.load_report_payload(reaction_dir) is None

    report_path.write_text(
        json.dumps(
            orca_artifact_payload(
                job_id="run_done",
                run_id="run_done",
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
    assert queue_orphans.terminal_report_data(reaction_dir) == (
        QueueStatus.COMPLETED.value,
        "run_done",
        "2026-03-10T04:59:59+00:00",
        None,
    )

    report_path.write_text(
        json.dumps(
            orca_artifact_payload(
                job_id="run_fail",
                run_id="run_fail",
                reaction_dir=str(reaction_dir),
                status="failed",
                final_result={
                    "status": "failed",
                    "completed_at": "2026-03-10T04:58:00+00:00",
                    "reason": "orca_crash",
                },
            )
        ),
        encoding="utf-8",
    )
    assert queue_orphans.terminal_report_data(reaction_dir) == (
        QueueStatus.FAILED.value,
        "run_fail",
        "2026-03-10T04:58:00+00:00",
        "orca_crash",
    )

    report_path.write_text(
        json.dumps(
            orca_artifact_payload(
                job_id="run_live",
                run_id="run_live",
                reaction_dir=str(reaction_dir),
                status="running",
            )
        ),
        encoding="utf-8",
    )
    assert queue_orphans.terminal_report_data(reaction_dir) is None


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


def test_list_queue_normalizes_common_fields_for_partial_entries(tmp_path: Path) -> None:
    root = tmp_path / "queue_root"
    root.mkdir()
    _save_entries(
        root,
        [
            queue_entries.entry_from_json_payload(
                {
                    "queue_id": "q_partial",
                    "status": QueueStatus.PENDING.value,
                    "metadata": {
                        "reaction_dir": str(root / "rxn"),
                        "force": False,
                    },
                }
            ),
        ],
    )

    entries = queue_adapter.list_queue(root)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.app_name == "orca_auto_orca"
    assert entry.task_id == "q_partial"
    assert entry.task_kind == "orca_run_inp"
    assert entry.engine == "orca"
    assert entry.metadata["reaction_dir"] == str(root / "rxn")
    assert entry.metadata["force"] is False


def test_queue_entry_accessors_read_common_fields_from_metadata(tmp_path: Path) -> None:
    entry = queue_entries.entry_from_json_payload(
        {
            "queue_id": "q_meta",
            "status": "PENDING",
            "priority": 7,
            "metadata": {
                "reaction_dir": str(tmp_path / "rxn"),
                "force": True,
            },
        }
    )

    assert queue_adapter.queue_entry_id(entry) == "q_meta"
    assert queue_adapter.queue_entry_task_id(entry) == "q_meta"
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
                    "status": QueueStatus.RUNNING.value,
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
    assert payload[0]["task_id"] == "q_backend"
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
                "run_id": "run_done",
                "status": RunStatus.COMPLETED.value,
                "updated_at": "2026-03-10T02:00:00+00:00",
                "final_result": {"completed_at": "2026-03-10T01:59:00+00:00"},
            }
        if reaction_dir == failed_dir:
            return {
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
            "orca_auto.orca.queue.orphans.active_lock_pid",
            return_value=None,
        ),
        patch(
            "orca_auto.orca.queue.orphans.load_state",
            side_effect=_load_state,
        ),
        patch(
            "orca_auto.orca.queue.orphans.terminal_report_data",
            return_value=None,
        ),
    ):
        changed = queue_orphans.reconcile_orphaned_running_entries(root)

    assert changed == 3
    entries = {entry.queue_id: entry for entry in queue_adapter.list_queue(root)}
    assert entries["q_done"].status == QueueStatus.COMPLETED
    assert queue_adapter.queue_entry_run_id(entries["q_done"]) == "run_done"
    assert entries["q_fail"].status == QueueStatus.FAILED
    assert entries["q_fail"].error == "orca_crash"
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
            "orca_auto.orca.queue.orphans.active_lock_pid",
            side_effect=lambda reaction_dir: 999 if reaction_dir == locked_dir else None,
        ),
    ):
        changed = queue_orphans.reconcile_orphaned_running_entries(root)

    assert changed == 0
    entries = {entry.queue_id: entry for entry in queue_adapter.list_queue(root)}
    assert entries["q_blank"].status == QueueStatus.RUNNING
    assert entries["q_locked"].status == QueueStatus.RUNNING


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
