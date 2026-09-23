"""ORCA lifecycle contracts after removing generic callback mediation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.config import AppConfig, OrcaRuntimeConfig
from orca_auto.orca.queue import cancellation, replay
from orca_auto.orca.queue.models import OrcaRunningJob
from orca_auto.orca.queue.worker import OrcaQueueWorker
from tests.process_helpers import FakeManagedProcess


def _entry(root: Path) -> QueueEntry:
    return QueueEntry(
        queue_id="queue-1",
        app_name="orca_auto_orca",
        engine="orca",
        task_kind="orca_single",
        task_id="task-1",
        status=QueueStatus.RUNNING,
        metadata={"reaction_dir": str(root / "reaction")},
    )


def _job(root: Path, *, task_id: str | None = "task-1") -> OrcaRunningJob:
    return OrcaRunningJob(
        queue_root=root,
        queue_id="queue-1",
        reaction_dir=str(root / "reaction"),
        task_id=task_id,
        process=FakeManagedProcess(poll_result=0),
        admission_token="slot-1",
    )


def _worker(root: Path) -> OrcaQueueWorker:
    return OrcaQueueWorker(
        AppConfig(runtime=OrcaRuntimeConfig(allowed_root=str(root))), "config.yaml"
    )


@pytest.mark.parametrize("metadata_key", ["job_dir", "reaction_dir"])
def test_attach_preserves_admission_identity_and_work_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_key: str,
) -> None:
    worker = _worker(tmp_path)
    entry = replace(_entry(tmp_path), metadata={metadata_key: " /chosen/path "})
    attach = Mock(return_value=True)
    upsert = Mock()
    monkeypatch.setattr(replay, "update_slot_metadata", attach)
    monkeypatch.setattr(replay.worker_tracking, "upsert_running_job_record", upsert)
    assert worker._on_worker_process_started(
        tmp_path,
        entry,
        process=FakeManagedProcess(pid=123),
        admission_token="slot-1",
    )
    attach.assert_called_once_with(
        worker.admission_root,
        "slot-1",
        state="active",
        queue_id="queue-1",
        app_name="orca_auto_orca",
        task_id="task-1",
        owner_pid=123,
        work_dir="/chosen/path",
    )
    upsert.assert_called_once_with(worker.cfg, entry)


def test_rejected_attach_stops_child_then_marks_selected_generation_then_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(tmp_path)
    entry = _entry(tmp_path)
    process = FakeManagedProcess()
    events: list[str] = []
    monkeypatch.setattr(replay, "update_slot_metadata", Mock(return_value=False))
    monkeypatch.setattr(
        replay, "terminate_process", Mock(side_effect=lambda _proc: events.append("stop"))
    )
    mark = Mock(side_effect=lambda *_args, **_kwargs: events.append("mark"))
    monkeypatch.setattr(replay, "mark_failed", mark)
    monkeypatch.setattr(worker, "_release_admission_slot", lambda _token: events.append("release"))
    upsert = Mock()
    monkeypatch.setattr(replay.worker_tracking, "upsert_running_job_record", upsert)
    assert not worker._on_worker_process_started(
        tmp_path,
        entry,
        process=process,
        admission_token="slot-1",
    )
    assert events == ["stop", "mark", "release"]
    mark.assert_called_once_with(
        tmp_path,
        "queue-1",
        error="admission_slot_missing",
        expected_entry=entry,
    )
    upsert.assert_not_called()


def test_running_record_failure_does_not_untrack_started_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(tmp_path)
    monkeypatch.setattr(replay, "update_slot_metadata", Mock(return_value=True))
    monkeypatch.setattr(
        replay.worker_tracking,
        "upsert_running_job_record",
        Mock(side_effect=OSError("record unavailable")),
    )
    assert worker._on_worker_process_started(
        tmp_path,
        _entry(tmp_path),
        process=FakeManagedProcess(),
        admission_token="slot-1",
    )


@pytest.mark.parametrize(
    ("rc", "cancel", "marker_name", "expected_status"),
    [
        (0, False, "mark_completed", "completed"),
        (17, False, "mark_failed", "failed"),
        (0, True, "mark_cancelled", "cancelled"),
    ],
)
def test_terminal_mark_keeps_premark_context_when_row_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rc: int,
    cancel: bool,
    marker_name: str,
    expected_status: str,
) -> None:
    entry = _entry(tmp_path)
    current: list[QueueEntry | None] = [entry]
    monkeypatch.setattr(replay, "queue_entry_by_id", lambda _root, _qid: current[0])
    run_id = Mock(return_value="run-1")
    monkeypatch.setattr(replay.worker_tracking, "get_run_id_from_state", run_id)
    cancel_requested = Mock(return_value=cancel)
    monkeypatch.setattr(replay, "get_cancel_requested", cancel_requested)

    def remove_row(*_args: object, **_kwargs: object) -> bool:
        current[0] = None
        return True

    marker = Mock(side_effect=remove_row)
    monkeypatch.setattr(replay, marker_name, marker)
    result = replay.mark_terminal_queue_entry("queue-1", _job(tmp_path), rc=rc)
    assert result == replay.TerminalQueueMarkResult(
        True, expected_status, "task-1", entry, tmp_path, "run-1"
    )
    assert marker.call_args.kwargs["expected_entry"] is entry
    assert marker.call_args.kwargs["expected_task_id"] == "task-1"
    cancel_requested.assert_called_once_with(
        tmp_path, "queue-1", expected_entry=entry, expected_task_id="task-1"
    )
    run_id.assert_called_once_with(str(tmp_path / "reaction"), expected_job_id="task-1")


@pytest.mark.parametrize("current_kind", ["missing", "pending", "replacement"])
def test_terminal_mark_does_not_touch_missing_nonrunning_or_new_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current_kind: str,
) -> None:
    current = (
        None
        if current_kind == "missing"
        else replace(
            _entry(tmp_path),
            status=QueueStatus.PENDING if current_kind == "pending" else QueueStatus.RUNNING,
            task_id="task-new" if current_kind == "replacement" else "task-1",
        )
    )
    monkeypatch.setattr(replay, "queue_entry_by_id", lambda _root, _qid: current)
    run_id = Mock()
    monkeypatch.setattr(replay.worker_tracking, "get_run_id_from_state", run_id)
    result = replay.mark_terminal_queue_entry("queue-1", _job(tmp_path), rc=0)
    assert not result.marked
    assert result.expected_job_id == "task-1"
    assert result.current_entry is current
    run_id.assert_not_called()


def test_terminal_mark_reports_rejected_generation_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _entry(tmp_path)
    monkeypatch.setattr(replay, "queue_entry_by_id", lambda _root, _qid: current)
    monkeypatch.setattr(replay.worker_tracking, "get_run_id_from_state", Mock(return_value="run-1"))
    monkeypatch.setattr(replay, "get_cancel_requested", Mock(return_value=False))
    monkeypatch.setattr(replay, "mark_completed", Mock(return_value=False))
    result = replay.mark_terminal_queue_entry("queue-1", _job(tmp_path), rc=0)
    assert result == replay.TerminalQueueMarkResult(
        False, None, "task-1", current, tmp_path, "run-1"
    )


@pytest.mark.parametrize("failure", ["terminate", "surviving", "recover", "mark"])
def test_cancel_failure_retains_slot_and_retry_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    worker = _worker(tmp_path)
    job = _job(tmp_path)
    if failure == "surviving":
        job.process = FakeManagedProcess(poll_result=None)
    monkeypatch.setattr(
        replay,
        "terminate_process",
        Mock(
            return_value=True,
            side_effect=OSError("stop failed") if failure == "terminate" else None,
        ),
    )
    monkeypatch.setattr(
        replay,
        "recover_slot_engine_process",
        Mock(
            side_effect=OSError("recover failed") if failure == "recover" else None,
        ),
    )
    monkeypatch.setattr(replay, "queue_entry_by_id", Mock(return_value=_entry(tmp_path)))
    mark = Mock(side_effect=OSError("mark failed"))
    monkeypatch.setattr(replay, "mark_cancelled", mark)
    release = Mock()
    monkeypatch.setattr(worker, "_release_admission_slot", release)
    assert not cancellation.cancel_running_job(worker, "queue-1", job)
    release.assert_not_called()
    if failure in {"terminate", "surviving", "recover"}:
        mark.assert_not_called()
    if failure in {"recover", "mark"}:
        assert job.terminal_finalize_pending


def test_reconciliation_keeps_scoped_and_legacy_live_slot_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(tmp_path)
    events: list[str] = []
    root = tmp_path / "queue"
    protected_keys = {("queue-live", str(tmp_path / "reaction"))}
    protected_ids = {"queue-legacy"}
    monkeypatch.setattr(
        replay, "recover_orphaned_engine_slots", lambda *_args, **_kwargs: events.append("recover")
    )
    monkeypatch.setattr(replay, "queue_entries_with_roots", lambda _cfg: [])
    monkeypatch.setattr(
        replay,
        "live_queue_slot_keys_for_slots",
        lambda *_args, **_kwargs: (protected_keys, protected_ids),
    )
    monkeypatch.setattr(replay, "reconcile_stale_slots", lambda _root: events.append("stale"))
    monkeypatch.setattr(replay, "queue_roots", lambda _cfg: (root,))
    reconcile = Mock(side_effect=lambda *_args, **_kwargs: events.append("orphans"))
    monkeypatch.setattr(replay, "reconcile_orphaned_running_entries", reconcile)
    replay.reconcile_worker_state(worker)
    assert events == ["recover", "stale", "orphans"]
    reconcile.assert_called_once_with(
        root,
        ignore_worker_pid=True,
        protected_queue_keys=protected_keys,
        protected_queue_ids=protected_ids,
    )
