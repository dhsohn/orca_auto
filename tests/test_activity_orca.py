from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto import activity
from orca_auto.activity import _cancel as _activity_cancel
from orca_auto.activity import _list as _activity_list
from orca_auto.activity import _orca as _activity_orca
from orca_auto.core import activity as _activity_model
from orca_auto.core.app_ids import ORCA_AUTO_ORCA_SOURCE
from orca_auto.core.config import discovery
from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.types import QueueEntry, QueueStatus


def _shared_config(tmp_path: Path) -> tuple[Path, Path]:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")
    return config_path, runs_root


def test_activity_helper_edges_and_discovery_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.core.utils import mapping_or_empty

    assert mapping_or_empty({"a": 1}) == {"a": 1}
    assert mapping_or_empty(["not", "mapping"]) == {}

    empty_timestamp = _activity_model.ActivityRecord(
        "empty", "job", "x", "running", "", "", "", "", ""
    )
    bad_timestamp = _activity_model.ActivityRecord(
        "bad", "job", "x", "running", "", "", "bad", "bad", ""
    )
    valid_timestamp = _activity_model.ActivityRecord(
        "valid",
        "job",
        "x",
        "running",
        "",
        "",
        "2026-04-26T00:00:00Z",
        "2026-04-26T00:00:00+09:00",
        "",
    )
    naive_timestamp = _activity_model.ActivityRecord(
        "naive",
        "job",
        "x",
        "running",
        "",
        "",
        "2026-04-26T00:00:00",
        "2026-04-26T00:00:00",
        "",
    )
    assert _activity_model.sort_key(empty_timestamp) < _activity_model.sort_key(valid_timestamp)
    assert _activity_model.sort_key(bad_timestamp) < _activity_model.sort_key(valid_timestamp)
    assert _activity_model.sort_key(naive_timestamp)[0].tzinfo is not None
    assert _activity_model.unique_texts([" a ", "", "a", "b"]) == ("a", "b")
    assert _activity_model.mapping_text({"key": " value "}, "key") == "value"
    assert _activity_model.path_aliases("", root=tmp_path) == ()


@pytest.mark.parametrize(
    ("run_lock_held", "expected_status"),
    [(False, "pending"), (True, "running")],
)
def test_orca_records_do_not_reconcile_or_mutate_orphaned_running_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_lock_held: bool,
    expected_status: str,
) -> None:
    allowed = tmp_path / "orca"
    allowed.mkdir()
    reaction_dir = allowed / "rxn-read-only"
    reaction_dir.mkdir()
    entry = QueueEntry(
        queue_id="q-read-only",
        app_name="orca_auto_orca",
        task_id="task-read-only",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.RUNNING,
        priority=1,
        enqueued_at="2026-04-26T00:00:00+00:00",
        started_at="2026-04-26T00:01:00+00:00",
        metadata={"reaction_dir": str(reaction_dir)},
    )
    queue_store.save_entries(allowed, [entry])
    queue_path = allowed / queue_store.QUEUE_FILE_NAME
    before = queue_path.read_bytes()

    from orca_auto.orca import run_snapshot

    monkeypatch.setattr(
        _activity_orca,
        "engine_runtime_paths",
        lambda config_path: {"allowed_root": allowed},
    )
    monkeypatch.setattr(run_snapshot, "collect_run_snapshots", lambda root, **kwargs: [])
    monkeypatch.setattr(
        _activity_orca,
        "run_lock_is_held",
        lambda *args, **kwargs: run_lock_held,
    )

    rows = _activity_orca.orca_records(config_path="/tmp/cfg.yaml")

    assert queue_path.read_bytes() == before
    (persisted,) = queue_store.load_entries(allowed)
    assert persisted.status == QueueStatus.RUNNING
    assert len(rows) == 1
    assert rows[0].activity_id == entry.queue_id
    assert rows[0].status == expected_status


def test_orca_records_merge_queue_entries_and_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "orca"
    allowed.mkdir()
    reaction_dir = allowed / "rxn-1"
    reaction_dir.mkdir()
    orphan_dir = allowed / "orphan"
    orphan_dir.mkdir()
    entries = [
        QueueEntry(
            queue_id="q-1",
            app_name="orca_auto_orca",
            task_id="task-1",
            task_kind="orca_run_inp",
            engine="orca",
            status=QueueStatus.RUNNING,
            priority=3,
            enqueued_at="2026-04-26T00:00:00+00:00",
            started_at="2026-04-26T00:01:00+00:00",
            metadata={
                "run_id": "run-1",
                "reaction_dir": str(reaction_dir),
                "job_type": "opt",
                "selected_inp": "rxn.inp",
            },
        ),
        QueueEntry(
            queue_id="q-2",
            app_name="orca_auto_orca",
            task_id="task-2",
            task_kind="orca_run_inp",
            engine="orca",
            status=QueueStatus.RUNNING,
            priority=4,
            enqueued_at="2026-04-26T00:02:00+00:00",
            cancel_requested=True,
            metadata={"reaction_dir": str(tmp_path / "missing")},
        ),
        QueueEntry(
            queue_id="xtb-foreign",
            app_name="orca_auto_xtb",
            task_id="xtb-task",
            task_kind="xtb_sp",
            engine="xtb",
            status=QueueStatus.PENDING,
            priority=1,
            enqueued_at="2026-04-26T00:03:00+00:00",
            metadata={"job_type": "sp", "job_dir": str(tmp_path / "xtb")},
        ),
    ]
    snapshots = [
        SimpleNamespace(
            key="snap-1",
            run_id="run-1",
            reaction_dir=reaction_dir,
            status="completed",
            name="tracked-name",
            completed_at="2026-04-26T01:00:00+00:00",
            updated_at="",
            started_at="2026-04-26T00:00:00+00:00",
            attempts=2,
            selected_inp_name="rxn.inp",
            job_type="opt",
        ),
        SimpleNamespace(
            key="snap-2",
            run_id="run-2",
            reaction_dir=orphan_dir,
            status="failed",
            name="",
            completed_at="",
            updated_at="2026-04-26T02:00:00+00:00",
            started_at="2026-04-26T01:30:00+00:00",
            attempts=1,
            selected_inp_name="orphan.inp",
            job_type="sp",
        ),
    ]
    reconciled: list[Path] = []

    from orca_auto.orca import run_snapshot
    from orca_auto.orca.queue import adapter as queue_adapter

    monkeypatch.setattr(
        _activity_orca,
        "engine_runtime_paths",
        lambda config_path: {"allowed_root": allowed},
    )
    monkeypatch.setattr(
        queue_adapter, "reconcile_orphaned_running_entries", lambda root: reconciled.append(root)
    )
    monkeypatch.setattr(queue_adapter, "list_queue", lambda root: entries)
    monkeypatch.setattr(run_snapshot, "collect_run_snapshots", lambda root, **kwargs: snapshots)

    rows = _activity_orca.orca_records(
        config_path="/tmp/cfg.yaml",
    )

    assert reconciled == []
    by_id = {row.activity_id: row for row in rows}
    assert "xtb-foreign" not in by_id
    assert by_id["q-1"].status == "completed"
    assert by_id["q-1"].label == "tracked-name"
    assert by_id["q-1"].metadata["elapsed_started_at"] == "2026-04-26T00:01:00+00:00"
    assert by_id["q-2"].status == "cancel_requested"
    assert by_id["q-2"].metadata["elapsed_started_at"] == "2026-04-26T00:02:00+00:00"
    assert by_id["run-2"].status == "failed"
    assert by_id["run-2"].metadata["elapsed_started_at"] == "2026-04-26T01:30:00+00:00"
    assert by_id["run-2"].metadata["selected_inp_name"] == "orphan.inp"


def test_orca_records_suppress_stale_snapshot_for_terminal_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A cancelled queue entry whose run state still reads "running" must not keep
    # showing the job as in progress: the stale snapshot is superseded by the
    # terminal queue outcome and should not be listed as a separate active row.
    allowed = tmp_path / "orca"
    allowed.mkdir()
    reaction_dir = allowed / "ts3"
    reaction_dir.mkdir()
    entries = [
        QueueEntry(
            queue_id="q-cancel",
            app_name="orca_auto_orca",
            task_id="task-ts3",
            task_kind="orca_run_inp",
            engine="orca",
            status=QueueStatus.CANCELLED,
            priority=3,
            enqueued_at="2026-04-26T00:00:00+00:00",
            started_at="2026-04-26T00:01:00+00:00",
            finished_at="2026-04-26T00:05:00+00:00",
            metadata={"reaction_dir": str(reaction_dir)},
        ),
    ]
    snapshots = [
        SimpleNamespace(
            key="snap-stale",
            run_id="",
            reaction_dir=reaction_dir,
            status="running",
            name="ts3",
            completed_at="",
            updated_at="2026-04-26T00:04:00+00:00",
            started_at="2026-04-26T00:01:00+00:00",
            attempts=1,
            selected_inp_name="ts3.inp",
            job_type="optts",
        ),
    ]

    from orca_auto.orca import run_snapshot
    from orca_auto.orca.queue import adapter as queue_adapter

    monkeypatch.setattr(
        _activity_orca,
        "engine_runtime_paths",
        lambda config_path: {"allowed_root": allowed},
    )
    monkeypatch.setattr(queue_adapter, "reconcile_orphaned_running_entries", lambda root: None)
    monkeypatch.setattr(queue_adapter, "list_queue", lambda root: entries)
    monkeypatch.setattr(run_snapshot, "collect_run_snapshots", lambda root, **kwargs: snapshots)

    rows = _activity_orca.orca_records(
        config_path="/tmp/cfg.yaml",
    )

    # Only the cancelled queue record remains; the stale running snapshot is gone.
    assert {row.activity_id: row.status for row in rows} == {"q-cancel": "cancelled"}


def test_orca_records_keep_live_snapshot_despite_terminal_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A genuinely live re-run that shares a reaction dir with an older terminal
    # queue entry must NOT be suppressed: the live run lock means it is in progress,
    # so the snapshot row is kept alongside the terminal queue row.
    allowed = tmp_path / "orca"
    allowed.mkdir()
    reaction_dir = allowed / "ts4"
    reaction_dir.mkdir()
    entries = [
        QueueEntry(
            queue_id="q-done",
            app_name="orca_auto_orca",
            task_id="task-ts4",
            task_kind="orca_run_inp",
            engine="orca",
            status=QueueStatus.COMPLETED,
            priority=3,
            enqueued_at="2026-04-26T00:00:00+00:00",
            started_at="2026-04-26T00:01:00+00:00",
            finished_at="2026-04-26T00:05:00+00:00",
            metadata={"reaction_dir": str(reaction_dir)},
        ),
    ]
    snapshots = [
        SimpleNamespace(
            key="snap-live",
            run_id="",
            reaction_dir=reaction_dir,
            status="running",
            name="ts4",
            completed_at="",
            updated_at="2026-04-26T00:10:00+00:00",
            started_at="2026-04-26T00:09:00+00:00",
            attempts=1,
            selected_inp_name="ts4.inp",
            job_type="optts",
        ),
    ]

    from orca_auto.orca import run_snapshot
    from orca_auto.orca.queue import adapter as queue_adapter

    monkeypatch.setattr(
        _activity_orca,
        "engine_runtime_paths",
        lambda config_path: {"allowed_root": allowed},
    )
    monkeypatch.setattr(queue_adapter, "reconcile_orphaned_running_entries", lambda root: None)
    monkeypatch.setattr(queue_adapter, "list_queue", lambda root: entries)
    monkeypatch.setattr(run_snapshot, "collect_run_snapshots", lambda root, **kwargs: snapshots)
    # A live run lock holds the dir -> the snapshot is a genuine in-progress re-run.
    monkeypatch.setattr(_activity_orca, "run_lock_is_held", lambda *a, **k: True)

    rows = _activity_orca.orca_records(
        config_path="/tmp/cfg.yaml",
    )

    # Both the terminal queue row and the live running snapshot row are present.
    assert {row.activity_id: row.status for row in rows} == {
        "q-done": "completed",
        "ts4": "running",
    }


def test_snapshot_display_status_marks_dead_running_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.orca.run_snapshot import RunSnapshot

    def _snap(status: str) -> RunSnapshot:
        return RunSnapshot(
            key="k",
            name="rxn",
            reaction_dir=Path("/tmp/rxn"),
            run_id="r",
            status=status,
            started_at="",
            updated_at="",
            completed_at="",
            selected_inp_name="",
            attempts=0,
        )

    running = _snap("running")

    # No live run lock -> the run is gone; show it as failed, not in progress.
    monkeypatch.setattr(_activity_orca, "run_lock_is_held", lambda *a, **k: False)
    assert _activity_orca._snapshot_display_status(running) == "failed"

    # A live run lock -> genuinely running, leave it as running.
    monkeypatch.setattr(_activity_orca, "run_lock_is_held", lambda *a, **k: True)
    assert _activity_orca._snapshot_display_status(running) == "running"

    # Terminal statuses are never reinterpreted, regardless of the lock.
    done = _snap("completed")
    monkeypatch.setattr(_activity_orca, "run_lock_is_held", lambda *a, **k: False)
    assert _activity_orca._snapshot_display_status(done) == "completed"


def test_match_activity_record_and_cancel_error_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        activity.ActivityRecord(
            "a",
            "job",
            "orca",
            "running",
            "A",
            "orca_auto_orca",
            "",
            "",
            "target",
            aliases=("same",),
        ),
        activity.ActivityRecord(
            "b",
            "job",
            "orca",
            "running",
            "B",
            "orca_auto_orca",
            "",
            "",
            "target",
            aliases=("same",),
        ),
    ]
    with pytest.raises(ValueError, match="empty"):
        _activity_cancel.match_activity_record(records, "")
    with pytest.raises(ValueError, match="Ambiguous activity target"):
        _activity_cancel.match_activity_record(records, "target")
    with pytest.raises(ValueError, match="Ambiguous activity target"):
        _activity_cancel.match_activity_record(records, "same")
    with pytest.raises(LookupError, match="not found"):
        _activity_cancel.match_activity_record(records, "missing")

    def collect_one(record: activity.ActivityRecord) -> None:
        monkeypatch.setattr(
            _activity_list, "collect_activity_records", lambda *args, **kwargs: [record]
        )

    monkeypatch.setattr(
        _activity_list,
        "resolve_activity_sources",
        lambda request: _activity_model.ResolvedActivitySources(None),
    )

    collect_one(
        activity.ActivityRecord(
            "orca", "job", "orca", "running", "O", ORCA_AUTO_ORCA_SOURCE, "", "", "orca-q"
        )
    )
    with pytest.raises(ValueError, match="orca_auto_config"):
        activity.cancel_activity(target="orca-q")

    collect_one(
        activity.ActivityRecord("bad", "job", "x", "running", "B", "unknown", "", "", "bad-q")
    )
    with pytest.raises(ValueError, match="Unsupported activity source"):
        activity.cancel_activity(target="bad-q")


def test_cancel_activity_routes_orca_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    records = {
        "orca-q": activity.ActivityRecord(
            "orca-q", "job", "orca", "running", "O", ORCA_AUTO_ORCA_SOURCE, "", "", "orca-q"
        ),
    }
    monkeypatch.setattr(
        _activity_list,
        "collect_activity_records",
        lambda *args, **kwargs: list(records.values()),
    )
    monkeypatch.setattr(
        _activity_cancel, "cancel_orca_target", lambda **kwargs: {"status": "", **kwargs}
    )

    orca_payload = activity.cancel_activity(target="orca-q", orca_config="/tmp/orca.yaml")

    assert orca_payload["status"] == "failed"
    assert orca_payload["result"]["target"] == "orca-q"
    assert orca_payload["result"]["config_path"] == str(Path("/tmp/orca.yaml").resolve())


def test_list_activities_autodiscovers_defaults_when_no_args(monkeypatch) -> None:
    monkeypatch.setattr(
        discovery,
        "resolve_shared_config_path",
        lambda explicit: "/tmp/orca_auto.yaml",
    )
    captured: dict[str, Any] = {}

    def fake_collect(
        resolved: activity.ResolvedActivitySources, request: activity.ActivityListRequest
    ) -> list[activity.ActivityRecord]:
        captured.update(vars(resolved))
        assert request.indexed
        return []

    monkeypatch.setattr(_activity_list, "collect_activity_records", fake_collect)

    payload = activity.list_activities()

    assert payload["count"] == 0
    assert payload["sources"] == {
        "orca_config": "/tmp/orca_auto.yaml",
    }
    assert captured["orca_config"] == "/tmp/orca_auto.yaml"
