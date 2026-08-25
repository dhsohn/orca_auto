from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from orca_auto.orca import run_cleanup, run_snapshot
from orca_auto.orca.queue import adapter as queue_adapter
from orca_auto.orca.queue.terminal_replay import terminal_replay_marker_from_entry
from orca_auto.orca.run_snapshot import RunSnapshot
from orca_auto.orca.state import load_state, save_state, state_path


def _snapshot(
    reaction_dir: Path,
    *,
    run_id: str = "run_1",
    name: str = "rxn",
    status: str = "running",
    selected_inp_name: str = "calc.inp",
) -> RunSnapshot:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    directory_stat = reaction_dir.stat()
    return RunSnapshot(
        key=f"key-{name}",
        name=name,
        reaction_dir=reaction_dir,
        run_id=run_id,
        status=status,
        started_at="2026-03-10T00:00:00+00:00",
        updated_at="2026-03-10T01:00:00+00:00",
        completed_at="2026-03-10T01:00:00+00:00",
        selected_inp_name=selected_inp_name,
        attempts=2,
        reaction_dir_identity=(directory_stat.st_dev, directory_stat.st_ino),
    )


def _write_state(
    reaction_dir: Path,
    *,
    run_id: str,
    status: str,
    inp_name: str = "calc.inp",
) -> None:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "run_id": run_id,
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(reaction_dir / inp_name),
        "max_retries": 2,
        "status": status,
        "started_at": "2026-03-10T00:00:00+00:00",
        "updated_at": "2026-03-10T01:00:00+00:00",
        "attempts": [{"index": 1}],
        "final_result": {"status": status},
    }
    save_state(reaction_dir, state)


def test_resolved_path_text_handles_blank_and_resolve_failure(tmp_path: Path) -> None:
    assert run_cleanup._resolved_path_text("") == ""

    original = str(tmp_path / "job")
    with patch("pathlib.Path.resolve", side_effect=OSError("boom")):
        assert run_cleanup._resolved_path_text(original) == original


def test_clear_terminal_run_states_clears_tracked_and_untracked_terminal_states(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    organized_dir = tmp_path / "organized" / "project" / "rxn_tracked"
    untracked_dir = allowed_root / "untracked" / "rxn_failed"
    running_dir = allowed_root / "live" / "rxn_running"
    allowed_root.mkdir()

    _write_state(organized_dir, run_id="run_tracked", status="completed", inp_name="tracked.inp")
    _write_state(untracked_dir, run_id="run_failed", status="failed")
    _write_state(running_dir, run_id="run_running", status="running")
    (allowed_root / "job_locations.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "job_tracked",
                    "app_name": "orca_auto_orca",
                    "job_type": "orca_opt",
                    "status": "completed",
                    "original_run_dir": str(allowed_root / "project" / "rxn_tracked"),
                    "molecule_key": "rxn_tracked",
                    "selected_input_xyz": str(organized_dir / "tracked.inp"),
                    "latest_known_path": str(organized_dir),
                    "resource_request": {},
                    "resource_actual": {},
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )

    cleared = run_cleanup.clear_terminal_run_states(allowed_root)

    assert cleared == 2
    assert not state_path(organized_dir).exists()
    assert not state_path(untracked_dir).exists()
    assert state_path(running_dir).exists()


def test_clear_terminal_run_states_skips_missing_files_and_warns_on_unlink_error(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    missing_snapshot = _snapshot(
        allowed_root / "missing", run_id="run_missing", name="missing", status="completed"
    )
    failed_dir = allowed_root / "failed"
    success_dir = allowed_root / "success"
    running_dir = allowed_root / "running"
    _write_state(failed_dir, run_id="run_failed", status="failed")
    _write_state(success_dir, run_id="run_success", status="completed")
    _write_state(running_dir, run_id="run_running", status="running")
    snapshots = {
        snapshot.reaction_dir: snapshot
        for snapshot in run_snapshot.collect_run_snapshots(allowed_root)
    }
    failed_snapshot = snapshots[failed_dir]
    success_snapshot = snapshots[success_dir]
    running_snapshot = snapshots[running_dir]

    failed_state = state_path(failed_dir)
    success_state = state_path(success_dir)
    running_state = state_path(running_dir)

    original_unlink = os.unlink

    def fake_unlink(path: str | bytes, *, dir_fd: int | None = None) -> None:
        if dir_fd is not None:
            parent = Path(os.readlink(f"/proc/self/fd/{dir_fd}"))
            candidate = parent / os.fsdecode(path)
        else:
            candidate = Path(os.fsdecode(path))
        if candidate == failed_state:
            raise OSError("cannot remove")
        return original_unlink(path, dir_fd=dir_fd)

    with (
        patch(
            "orca_auto.orca.run_cleanup.collect_run_snapshots",
            return_value=[missing_snapshot, failed_snapshot, success_snapshot, running_snapshot],
        ),
        patch(
            "orca_auto.orca.run_cleanup.os.unlink",
            new=fake_unlink,
        ),
        patch("orca_auto.orca.run_cleanup.logger.warning") as warning,
    ):
        cleared = run_cleanup.clear_terminal_run_states(allowed_root)

    assert cleared == 1
    warning.assert_called_once()
    assert failed_state.exists()
    assert not success_state.exists()
    assert running_state.exists()


def test_clear_terminal_run_states_preserves_new_atomic_state_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_root = tmp_path / "runs"
    reaction_dir = allowed_root / "job"
    _write_state(reaction_dir, run_id="run-terminal", status="completed")
    original_collect = run_cleanup.collect_run_snapshots

    def _collect_then_publish_new_generation(root: Path) -> list[RunSnapshot]:
        snapshots = original_collect(root)
        _write_state(reaction_dir, run_id="run-running", status="running")
        return snapshots

    monkeypatch.setattr(
        run_cleanup,
        "collect_run_snapshots",
        _collect_then_publish_new_generation,
    )

    assert run_cleanup.clear_terminal_run_states(allowed_root) == 0
    current_state = load_state(reaction_dir)
    assert current_state is not None
    assert current_state["run_id"] == "run-running"
    assert current_state["status"] == "running"
    assert state_path(reaction_dir).exists()


def test_cleanup_serializes_atomic_state_publish_after_final_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_root = tmp_path / "runs"
    reaction_dir = allowed_root / "job"
    _write_state(reaction_dir, run_id="run-terminal", status="completed")
    identity_checked = threading.Event()
    writer_started = threading.Event()
    writer_finished = threading.Event()
    original_is_current = run_cleanup._snapshot_state_is_current

    def _publish_new_generation() -> None:
        assert identity_checked.wait(timeout=5)
        writer_started.set()
        _write_state(reaction_dir, run_id="run-running", status="running")
        writer_finished.set()

    writer = threading.Thread(target=_publish_new_generation)
    writer.start()

    def _checked_then_wait_for_writer(
        snapshot: RunSnapshot,
        directory_fd: int,
    ) -> bool:
        is_current = original_is_current(snapshot, directory_fd)
        identity_checked.set()
        assert writer_started.wait(timeout=5)
        assert not writer_finished.wait(timeout=0.2)
        return is_current

    monkeypatch.setattr(
        run_cleanup,
        "_snapshot_state_is_current",
        _checked_then_wait_for_writer,
    )
    try:
        assert run_cleanup.clear_terminal_run_states(allowed_root) == 1
    finally:
        writer.join(timeout=5)

    assert not writer.is_alive()
    assert writer_finished.is_set()
    current_state = load_state(reaction_dir)
    assert current_state is not None
    assert current_state["run_id"] == "run-running"
    assert current_state["status"] == "running"


def test_cleanup_and_renamed_state_writer_share_pinned_directory_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_root = tmp_path / "runs"
    reaction_dir = allowed_root / "job"
    renamed_dir = allowed_root / "job-renamed"
    _write_state(reaction_dir, run_id="run-terminal", status="completed")
    identity_checked = threading.Event()
    writer_started = threading.Event()
    writer_finished = threading.Event()
    writer_errors: list[BaseException] = []
    original_is_current = run_cleanup._snapshot_state_is_current

    def _publish_after_rename() -> None:
        try:
            assert identity_checked.wait(timeout=5)
            writer_started.set()
            _write_state(renamed_dir, run_id="run-running", status="running")
        except BaseException as exc:  # noqa: BLE001
            writer_errors.append(exc)
        finally:
            writer_finished.set()

    writer = threading.Thread(target=_publish_after_rename)
    writer.start()

    def _checked_then_rename(
        snapshot: RunSnapshot,
        directory_fd: int,
    ) -> bool:
        is_current = original_is_current(snapshot, directory_fd)
        reaction_dir.rename(renamed_dir)
        identity_checked.set()
        assert writer_started.wait(timeout=5)
        assert not writer_finished.wait(timeout=0.2)
        return is_current

    monkeypatch.setattr(
        run_cleanup,
        "_snapshot_state_is_current",
        _checked_then_rename,
    )
    try:
        assert run_cleanup.clear_terminal_run_states(allowed_root) == 1
    finally:
        writer.join(timeout=5)

    assert not writer.is_alive()
    assert writer_finished.is_set()
    assert writer_errors == []
    assert not reaction_dir.exists()
    current_state = load_state(renamed_dir)
    assert current_state is not None
    assert current_state["run_id"] == "run-running"
    assert current_state["status"] == "running"


def test_clear_terminal_entries_reports_queue_and_run_state_counts(tmp_path: Path) -> None:
    allowed_root = tmp_path / "orca_runs"
    allowed_root.mkdir()

    with (
        patch("orca_auto.orca.run_cleanup.clear_terminal", return_value=2),
        patch(
            "orca_auto.orca.run_cleanup.clear_terminal_run_states",
            return_value=3,
        ),
    ):
        assert run_cleanup.clear_terminal_entries(allowed_root) == (2, 3)


def test_clear_terminal_state_preserves_active_queue_finalization_window(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    reaction_dir = allowed_root / "rxn_finalizing"
    allowed_root.mkdir()
    entry = queue_adapter.enqueue(allowed_root, str(reaction_dir))
    assert queue_adapter.dequeue_next(allowed_root) is not None
    _write_state(reaction_dir, run_id="run-finalizing", status="completed")

    assert run_cleanup.clear_terminal_run_states(allowed_root) == 0
    assert state_path(reaction_dir).exists()
    [running] = queue_adapter.list_queue(allowed_root)
    assert running.queue_id == entry.queue_id
    assert running.status.value == "running"


def test_clear_terminal_state_rechecks_queue_marker_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    reaction_dir = allowed_root / "rxn_cleanup_race"
    allowed_root.mkdir()
    _write_state(reaction_dir, run_id="run-cleanup-race", status="completed")
    real_open = run_cleanup._open_stable_reaction_dir
    transition_written = False

    def open_then_terminalize(
        candidate: Path,
        root: Path,
        *,
        expected_identity: tuple[int, int] | None,
    ) -> int | None:
        nonlocal transition_written
        directory_fd = real_open(
            candidate,
            root,
            expected_identity=expected_identity,
        )
        if directory_fd is not None and not transition_written:
            transition_written = True
            entry = queue_adapter.enqueue(
                allowed_root,
                str(reaction_dir),
                task_id="task-cleanup-race",
            )
            running = queue_adapter.dequeue_next(allowed_root)
            assert running is not None
            assert queue_adapter.mark_failed(
                allowed_root,
                entry.queue_id,
                error="worker_start_error",
                expected_entry=running,
            )
        return directory_fd

    monkeypatch.setattr(run_cleanup, "_open_stable_reaction_dir", open_then_terminalize)

    assert run_cleanup.clear_terminal_run_states(allowed_root) == 0
    assert transition_written
    assert state_path(reaction_dir).exists()
    [terminal] = queue_adapter.list_queue(allowed_root)
    assert terminal_replay_marker_from_entry(terminal) is not None


def test_clear_terminal_entries_removes_cancelled_queue_stale_running_state(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    reaction_dir = allowed_root / "rxn_cancelled"
    allowed_root.mkdir()
    _write_state(reaction_dir, run_id="run_cancelled", status="running")

    entry = queue_adapter.enqueue(allowed_root, str(reaction_dir))
    queue_adapter.dequeue_next(allowed_root)
    queue_adapter.cancel(allowed_root, entry.queue_id)
    queue_adapter.requeue_running_entry(allowed_root, entry.queue_id)
    assert queue_adapter.update_metadata(
        allowed_root,
        entry.queue_id,
        {queue_adapter.TERMINAL_REPLAY_METADATA_KEY: None},
    )

    assert state_path(reaction_dir).exists()

    assert run_cleanup.clear_terminal_entries(allowed_root) == (1, 1)

    assert queue_adapter.list_queue(allowed_root) == []
    assert not state_path(reaction_dir).exists()


def test_clear_terminal_entries_removes_cancelled_queue_cancelled_run_state(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    reaction_dir = allowed_root / "rxn_cancelled_state"
    allowed_root.mkdir()
    _write_state(reaction_dir, run_id="run_cancelled", status="cancelled")

    entry = queue_adapter.enqueue(allowed_root, str(reaction_dir))
    queue_adapter.dequeue_next(allowed_root)
    assert queue_adapter.mark_cancelled(
        allowed_root,
        entry.queue_id,
        metadata_update={"run_id": "run_cancelled"},
    )
    assert run_cleanup.clear_terminal_entries(allowed_root) == (0, 0)
    assert len(queue_adapter.list_queue(allowed_root)) == 1
    assert state_path(reaction_dir).exists()

    assert queue_adapter.update_metadata(
        allowed_root,
        entry.queue_id,
        {queue_adapter.TERMINAL_REPLAY_METADATA_KEY: None},
    )

    assert state_path(reaction_dir).exists()

    assert run_cleanup.clear_terminal_entries(allowed_root) == (1, 1)

    assert queue_adapter.list_queue(allowed_root) == []
    assert not state_path(reaction_dir).exists()


def test_clear_terminal_entries_keeps_live_running_state_despite_terminal_queue(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    reaction_dir = allowed_root / "rxn_live_rerun"
    allowed_root.mkdir()
    _write_state(reaction_dir, run_id="run_live", status="running")

    entry = queue_adapter.enqueue(allowed_root, str(reaction_dir))
    queue_adapter.dequeue_next(allowed_root)
    queue_adapter.cancel(allowed_root, entry.queue_id)
    queue_adapter.requeue_running_entry(allowed_root, entry.queue_id)
    assert queue_adapter.update_metadata(
        allowed_root,
        entry.queue_id,
        {queue_adapter.TERMINAL_REPLAY_METADATA_KEY: None},
    )

    with patch("orca_auto.orca.run_cleanup.run_lock_is_held", return_value=True):
        assert run_cleanup.clear_terminal_entries(allowed_root) == (1, 0)

    assert queue_adapter.list_queue(allowed_root) == []
    assert state_path(reaction_dir).exists()


def test_clear_terminal_entries_keeps_state_when_same_dir_has_active_entry(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    reaction_dir = allowed_root / "rxn_active_retry"
    allowed_root.mkdir()
    _write_state(reaction_dir, run_id="run_active", status="running")

    old_entry = queue_adapter.enqueue(allowed_root, str(reaction_dir))
    queue_adapter.mark_completed(allowed_root, old_entry.queue_id)
    assert queue_adapter.update_metadata(
        allowed_root,
        old_entry.queue_id,
        {queue_adapter.TERMINAL_REPLAY_METADATA_KEY: None},
    )
    active_entry = queue_adapter.enqueue(allowed_root, str(reaction_dir), force=True)

    assert run_cleanup.clear_terminal_entries(allowed_root) == (1, 0)

    remaining = queue_adapter.list_queue(allowed_root)
    assert [entry.queue_id for entry in remaining] == [active_entry.queue_id]
    assert state_path(reaction_dir).exists()
