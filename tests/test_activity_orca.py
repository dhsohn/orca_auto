"""ORCA activity records read from real queue, state and index files.

``orca_records`` is driven through a real ``orca_auto.yaml`` (``config_path``),
real queue rows, ``job_state.json`` files and ``job_locations.json``; a live
run is one that holds ``run.lock`` for real.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orca_auto import activity
from orca_auto.activity import _cancel as _activity_cancel
from orca_auto.activity import _orca as _activity_orca
from orca_auto.activity import model as _activity_model
from orca_auto.core.activity_index import DB_NAME as ACTIVITY_INDEX_DB_NAME
from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca import run_status
from orca_auto.orca.app_ids import ORCA_AUTO_ORCA_SOURCE
from orca_auto.orca.config import AppConfig
from orca_auto.orca.job_locations import upsert_job_record
from orca_auto.orca.queue.adapter import enqueue, list_queue
from orca_auto.orca.run_lock import acquire_run_lock
from orca_auto.orca.run_snapshot import RunSnapshot
from orca_auto.orca.statuses import RunStatus
from tests.conftest import make_queue_entry, write_run_state


@pytest.fixture
def allowed(queue_root: Path) -> Path:
    return queue_root


@pytest.fixture
def orca_config(allowed: Path, config_path: Callable[..., Path]) -> str:
    return str(config_path(runs_root=allowed))


def _records_by_id(config: str) -> dict[str, _activity_model.ActivityRecord]:
    return {row.activity_id: row for row in _activity_orca.orca_records(config_path=config)}


def test_activity_helper_edges_and_discovery_paths(tmp_path: Path) -> None:
    from orca_auto.core.utils.coercion import mapping_or_empty

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
    allowed: Path,
    orca_config: str,
    run_lock_held: bool,
    expected_status: str,
) -> None:
    reaction_dir = allowed / "rxn-read-only"
    reaction_dir.mkdir()
    entry = make_queue_entry(
        queue_id="q-read-only",
        task_id="task-read-only",
        reaction_dir=reaction_dir,
        status=QueueStatus.RUNNING,
        priority=1,
        enqueued_at="2026-04-26T00:00:00+00:00",
        started_at="2026-04-26T00:01:00+00:00",
    )
    queue_store.save_entries(allowed, [entry])
    queue_path = allowed / queue_store.QUEUE_FILE_NAME
    before = queue_path.read_bytes()

    if run_lock_held:
        with acquire_run_lock(reaction_dir):
            rows = _activity_orca.orca_records(config_path=orca_config)
    else:
        rows = _activity_orca.orca_records(config_path=orca_config)

    # The listing reports the dead running row as pending (or running while a
    # child holds run.lock) without rewriting it: recovery belongs to the worker.
    assert queue_path.read_bytes() == before
    (persisted,) = queue_store.load_entries(allowed)
    assert persisted.status == QueueStatus.RUNNING
    assert len(rows) == 1
    assert rows[0].activity_id == entry.queue_id
    assert rows[0].status == expected_status


def test_orca_records_merge_queue_entries_and_snapshots(
    allowed: Path,
    orca_config: str,
    app_cfg: Callable[..., AppConfig],
) -> None:
    reaction_dir = allowed / "rxn-1"
    orphan_dir = allowed / "orphan"
    write_run_state(
        reaction_dir,
        status=RunStatus.COMPLETED,
        job_id="task-1",
        run_id="run-1",
        selected_inp=reaction_dir / "rxn.inp",
    )
    orphan = write_run_state(
        orphan_dir,
        status=RunStatus.FAILED,
        job_id="task-orphan",
        run_id="run-2",
        selected_inp=orphan_dir / "orphan.inp",
    )
    # The orphan directory is known only through the job-location index.
    upsert_job_record(
        app_cfg(runs_root=allowed),
        job_id="task-orphan",
        status="failed",
        job_dir=orphan_dir,
        job_type="sp",
        selected_input_xyz="",
    )
    entries = [
        make_queue_entry(
            queue_id="q-1",
            task_id="task-1",
            reaction_dir=reaction_dir,
            status=QueueStatus.RUNNING,
            priority=3,
            enqueued_at="2026-04-26T00:00:00+00:00",
            started_at="2026-04-26T00:01:00+00:00",
            metadata={"run_id": "run-1", "job_type": "opt", "selected_inp": "rxn.inp"},
        ),
        make_queue_entry(
            queue_id="q-2",
            task_id="task-2",
            reaction_dir=allowed / "missing",
            status=QueueStatus.RUNNING,
            priority=4,
            enqueued_at="2026-04-26T00:02:00+00:00",
            cancel_requested=True,
        ),
        QueueEntry(
            queue_id="other-foreign",
            app_name="orca_auto_other",
            task_id="other-task",
            task_kind="other_sp",
            engine="other",
            status=QueueStatus.PENDING,
            priority=1,
            enqueued_at="2026-04-26T00:03:00+00:00",
            metadata={"job_type": "sp", "job_dir": str(allowed / "other")},
        ),
    ]
    queue_store.save_entries(allowed, entries)
    queue_path = allowed / queue_store.QUEUE_FILE_NAME
    before = queue_path.read_bytes()

    by_id = _records_by_id(orca_config)

    assert queue_path.read_bytes() == before
    assert "other-foreign" not in by_id
    assert by_id["q-1"].status == "completed"
    assert by_id["q-1"].label == "rxn-1"
    assert by_id["q-1"].metadata["elapsed_started_at"] == "2026-04-26T00:01:00+00:00"
    assert by_id["q-2"].status == "cancel_requested"
    assert by_id["q-2"].metadata["elapsed_started_at"] == "2026-04-26T00:02:00+00:00"
    assert by_id["run-2"].status == "failed"
    assert by_id["run-2"].metadata["elapsed_started_at"] == orphan["started_at"]
    assert by_id["run-2"].metadata["selected_inp_name"] == "orphan.inp"
    assert set(by_id) == {"q-1", "q-2", "run-2"}
    # The per-job worker log is a top-level row key taken from queue metadata;
    # a row known only through its state file has none.
    assert by_id["q-1"].worker_log == ""
    assert by_id["run-2"].worker_log == ""
    assert by_id["q-1"].to_dict()["worker_log"] == ""


def test_clear_activities_reports_removed_worker_logs(allowed: Path, orca_config: str) -> None:
    from tests.conftest import enqueue_entry

    entry = enqueue_entry(
        allowed,
        make_queue_entry(
            queue_id="q-done", reaction_dir=allowed / "rxn-done", status=QueueStatus.COMPLETED
        ),
    )
    log = Path(entry.metadata["worker_log"])
    log.parent.mkdir(parents=True)
    log.write_text("done\n", encoding="utf-8")

    payload = activity.clear_activities(orca_config=orca_config)

    assert payload["cleared"]["orca_queue_entries"] == 1
    assert payload["removed_worker_logs"] == 1
    assert not log.exists()


def test_queue_record_lifts_worker_log_to_the_row(allowed: Path, orca_config: str) -> None:
    entry = make_queue_entry(
        queue_id="q-logged",
        reaction_dir=allowed / "rxn-logged",
        status=QueueStatus.RUNNING,
        metadata={"worker_log": str(allowed / "logs" / "q-logged.log")},
    )
    queue_store.save_entries(allowed, [entry])

    row = _records_by_id(orca_config)["q-logged"]

    assert row.worker_log == str(allowed / "logs" / "q-logged.log")
    payload = row.to_dict()
    assert payload["worker_log"] == row.worker_log
    assert "worker_log" not in payload["metadata"]


def test_orca_records_suppress_stale_snapshot_for_terminal_entry(
    allowed: Path,
    orca_config: str,
) -> None:
    # A cancelled queue entry whose run state still reads "running" must not keep
    # showing the job as in progress: the stale snapshot is superseded by the
    # terminal queue outcome and should not be listed as a separate active row.
    reaction_dir = allowed / "ts3"
    write_run_state(
        reaction_dir,
        status=RunStatus.RUNNING,
        job_id="task-ts3",
        selected_inp=reaction_dir / "ts3.inp",
    )
    queue_store.save_entries(
        allowed,
        [
            make_queue_entry(
                queue_id="q-cancel",
                task_id="task-ts3",
                reaction_dir=reaction_dir,
                status=QueueStatus.CANCELLED,
                priority=3,
                enqueued_at="2026-04-26T00:00:00+00:00",
                started_at="2026-04-26T00:01:00+00:00",
                finished_at="2026-04-26T00:05:00+00:00",
            )
        ],
    )

    rows = _activity_orca.orca_records(config_path=orca_config)

    # Only the cancelled queue record remains; the stale running snapshot is gone.
    assert {row.activity_id: row.status for row in rows} == {"q-cancel": "cancelled"}


def test_orca_records_keep_live_snapshot_despite_terminal_entry(
    allowed: Path,
    orca_config: str,
) -> None:
    # A genuinely live re-run that shares a reaction dir with an older terminal
    # queue entry must NOT be suppressed: the live run lock means it is in progress,
    # so the snapshot row is kept alongside the terminal queue row.
    reaction_dir = allowed / "ts4"
    live = write_run_state(
        reaction_dir,
        status=RunStatus.RUNNING,
        job_id="task-ts4-rerun",
        selected_inp=reaction_dir / "ts4.inp",
    )
    queue_store.save_entries(
        allowed,
        [
            make_queue_entry(
                queue_id="q-done",
                task_id="task-ts4",
                reaction_dir=reaction_dir,
                status=QueueStatus.COMPLETED,
                priority=3,
                enqueued_at="2026-04-26T00:00:00+00:00",
                started_at="2026-04-26T00:01:00+00:00",
                finished_at="2026-04-26T00:05:00+00:00",
            )
        ],
    )

    with acquire_run_lock(reaction_dir):
        rows = _activity_orca.orca_records(config_path=orca_config)

    # Both the terminal queue row and the live running snapshot row are present.
    assert {row.activity_id: row.status for row in rows} == {
        "q-done": "completed",
        live["run_id"]: "running",
    }


def test_snapshot_display_status_marks_dead_running_as_failed(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()

    def _snap(status: str) -> RunSnapshot:
        return RunSnapshot(
            key="k",
            name="rxn",
            reaction_dir=reaction_dir,
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
    assert run_status.snapshot_display_status(running) == "failed"

    # A live run lock -> genuinely running, leave it as running.
    with acquire_run_lock(reaction_dir):
        assert run_status.snapshot_display_status(running) == "running"

    # Terminal statuses are never reinterpreted, regardless of the lock.
    assert run_status.snapshot_display_status(_snap("completed")) == "completed"


def test_match_activity_record_and_cancel_error_edges() -> None:
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

    def cancel_with(record: activity.ActivityRecord) -> dict[str, Any]:
        request = activity.ActivityCancelRequest(
            target=record.cancel_target, sources=activity.ActivitySourceRequest()
        )
        return _activity_cancel.cancel_orca_activity(
            record, activity.ResolvedActivitySources(None), request
        )

    with pytest.raises(ValueError, match="orca_auto_config"):
        cancel_with(
            activity.ActivityRecord(
                "orca", "job", "orca", "running", "O", ORCA_AUTO_ORCA_SOURCE, "", "", "orca-q"
            )
        )
    with pytest.raises(ValueError, match="Unsupported activity source"):
        cancel_with(
            activity.ActivityRecord("bad", "job", "x", "running", "B", "unknown", "", "", "bad-q")
        )


def test_cancel_activity_routes_orca_targets(allowed: Path, orca_config: str) -> None:
    reaction_dir = allowed / "rxn"
    reaction_dir.mkdir()
    entry = enqueue(allowed, str(reaction_dir), task_id="task-cancel")

    orca_payload = activity.cancel_activity(target=entry.queue_id, orca_config=orca_config)

    assert orca_payload["status"] == "cancelled"
    assert orca_payload["activity_id"] == entry.queue_id
    assert orca_payload["source"] == ORCA_AUTO_ORCA_SOURCE
    assert orca_payload["result"]["queue_id"] == entry.queue_id
    assert orca_payload["result"]["job_id"] == "task-cancel"
    [cancelled] = list_queue(allowed)
    assert cancelled.status is QueueStatus.CANCELLED


def test_list_activities_autodiscovers_defaults_when_no_args(
    allowed: Path,
    orca_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ORCA_AUTO_CONFIG_ENV_VAR, orca_config)

    payload = activity.list_activities()

    assert payload["count"] == 0
    assert payload["activities"] == []
    assert payload["sources"] == {"orca_config": str(Path(orca_config).resolve())}
    # The default listing is the indexed projection, which materializes under
    # the runs root.
    assert (allowed / ACTIVITY_INDEX_DB_NAME).exists()
