"""ORCA activity records read from real queue and state files.

``catalog`` reads the runs root of a real ``orca_auto.yaml``: real queue rows
joined with each row's own ``job_state.json``. A live run is one that holds
``run.lock`` for real.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from orca_auto import activity
from orca_auto.activity import _cancel as _activity_cancel
from orca_auto.activity import _orca as _activity_orca
from orca_auto.activity import model as _activity_model
from orca_auto.cli import main as cli_main
from orca_auto.core.artifacts import QUEUE_FILE
from orca_auto.core.config.files import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.queue import persistence as queue_persistence
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.app_ids import ORCA_AUTO_ORCA_SOURCE
from orca_auto.orca.config import AppConfig
from orca_auto.orca.job_locations import upsert_job_record
from orca_auto.orca.queue.adapter import enqueue, list_queue
from orca_auto.orca.queue.entries import queue_entry_generation_token
from orca_auto.orca.run_lock import acquire_run_lock
from orca_auto.orca.state import save_state
from orca_auto.orca.statuses import RunStatus
from tests.conftest import make_queue_entry, write_run_state


@pytest.fixture
def allowed(queue_root: Path) -> Path:
    return queue_root


@pytest.fixture
def orca_config(allowed: Path, config_path: Callable[..., Path]) -> str:
    return str(config_path(runs_root=allowed))


def _records(root: Path) -> list[_activity_model.ActivityRecord]:
    return [record for _entry, record in _activity_orca.catalog(root)]


def _records_by_id(root: Path) -> dict[str, _activity_model.ActivityRecord]:
    return {row.activity_id: row for row in _records(root)}


def test_activity_helper_edges_and_discovery_paths(tmp_path: Path) -> None:
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
    assert _activity_model.path_aliases("", root=tmp_path) == ()


@pytest.mark.parametrize(
    ("run_lock_held", "expected_status"),
    [(False, "pending"), (True, "running")],
)
def test_catalog_does_not_reconcile_or_mutate_orphaned_running_entries(
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
    queue_persistence.save_entries(allowed, [entry])
    queue_path = allowed / QUEUE_FILE
    before = queue_path.read_bytes()

    if run_lock_held:
        with acquire_run_lock(reaction_dir):
            rows = _records(allowed)
    else:
        rows = _records(allowed)

    # The listing reports the dead running row as pending (or running while a
    # child holds run.lock) without rewriting it: recovery belongs to the worker.
    assert queue_path.read_bytes() == before
    (persisted,) = queue_persistence.load_entries(allowed)
    assert persisted.status == QueueStatus.RUNNING
    assert len(rows) == 1
    assert rows[0].activity_id == entry.queue_id
    assert rows[0].status == expected_status


def test_catalog_joins_queue_rows_with_their_own_state_only(
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
    # The orphan directory is known only through the job-location index, so it
    # is no row of the catalog.
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
    queue_persistence.save_entries(allowed, entries)
    queue_path = allowed / QUEUE_FILE
    before = queue_path.read_bytes()

    by_id = _records_by_id(allowed)

    assert queue_path.read_bytes() == before
    assert "other-foreign" not in by_id
    assert by_id["q-1"].status == "completed"
    assert by_id["q-1"].label == "rxn-1"
    assert by_id["q-1"].metadata["elapsed_started_at"] == "2026-04-26T00:01:00+00:00"
    assert by_id["q-2"].status == "cancel_requested"
    assert by_id["q-2"].metadata["elapsed_started_at"] == "2026-04-26T00:02:00+00:00"
    assert set(by_id) == {"q-1", "q-2"}
    assert orphan["run_id"] not in {alias for row in by_id.values() for alias in row.aliases}
    # The per-job worker log is a top-level row key taken from queue metadata.
    assert by_id["q-1"].worker_log == ""
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

    payload = activity.clear_activities(config_path=orca_config, runs_root=allowed)

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
    queue_persistence.save_entries(allowed, [entry])

    row = _records_by_id(allowed)["q-logged"]

    assert row.worker_log == str(allowed / "logs" / "q-logged.log")
    payload = row.to_dict()
    assert payload["worker_log"] == row.worker_log
    assert "worker_log" not in payload["metadata"]


@pytest.mark.parametrize("run_lock_held", [False, True], ids=["stale", "live"])
def test_catalog_lists_no_run_state_without_its_queue_row(
    allowed: Path,
    orca_config: str,
    run_lock_held: bool,
) -> None:
    # The root state belongs to another run than the finished queue row, so it
    # lends the row nothing and is no row of its own, whether or not a process
    # still holds its run lock.
    reaction_dir = allowed / "ts4"
    write_run_state(
        reaction_dir,
        status=RunStatus.RUNNING,
        job_id="task-ts4-rerun",
        selected_inp=reaction_dir / "ts4.inp",
    )
    queue_persistence.save_entries(
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

    if run_lock_held:
        with acquire_run_lock(reaction_dir):
            rows = _records(allowed)
    else:
        rows = _records(allowed)

    assert {row.activity_id: row.status for row in rows} == {"q-done": "completed"}
    assert rows[0].updated_at == "2026-04-26T00:05:00+00:00"


def _row(record: activity.ActivityRecord) -> tuple[QueueEntry, activity.ActivityRecord]:
    return make_queue_entry(queue_id=record.activity_id), record


def test_cancel_activity_reports_an_empty_or_unknown_target(
    allowed: Path, orca_config: str
) -> None:
    for target, error in (
        (" ", "Cancel target is empty."),
        ("missing", "Activity target not found: missing"),
    ):
        payload, message = activity.cancel_activity(target=target, runs_root=allowed)
        assert message == error
        assert payload == {
            "activity_id": "",
            "kind": "",
            "engine": "",
            "source": "",
            "label": "",
            "status": "failed",
            "cancel_target": "",
            "result": {
                "status": "failed",
                "reason": "target_not_found",
                "queue_id": "",
                "job_id": "",
                "reaction_dir": "",
            },
        }


def test_cancel_activity_routes_orca_targets(allowed: Path, orca_config: str) -> None:
    reaction_dir = allowed / "rxn"
    reaction_dir.mkdir()
    entry = enqueue(allowed, str(reaction_dir), task_id="task-cancel")

    orca_payload, error = activity.cancel_activity(target=entry.queue_id, runs_root=allowed)

    assert error == ""
    assert orca_payload["status"] == "cancelled"
    assert orca_payload["activity_id"] == entry.queue_id
    assert orca_payload["source"] == ORCA_AUTO_ORCA_SOURCE
    assert orca_payload["result"] == {
        "status": "cancelled",
        "reason": "",
        "queue_id": entry.queue_id,
        "job_id": "task-cancel",
        "reaction_dir": str(reaction_dir),
    }
    [cancelled] = list_queue(allowed)
    assert cancelled.status is QueueStatus.CANCELLED

    # A finished row, here named by the run ID its queue row records, is
    # reported, not cancelled again.
    queue_persistence.save_entries(
        allowed,
        [
            make_queue_entry(
                queue_id="q-done",
                reaction_dir=reaction_dir,
                status=QueueStatus.COMPLETED,
                metadata={"run_id": "run-done"},
            )
        ],
    )
    payload, error = activity.cancel_activity(target="run-done", runs_root=allowed)
    assert error == "queue target already terminal: q-done"
    assert payload["activity_id"] == "q-done"
    assert payload["result"]["reason"] == "already_terminal"
    assert payload["result"]["queue_id"] == "q-done"


@pytest.mark.parametrize(
    "alias", ["absolute", "relative", "basename", "working-directory", "trailing-slash"]
)
def test_cancel_activity_path_alias_prefers_active_generation(
    allowed: Path, orca_config: str, alias: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reaction_dir = allowed / "batch" / "water"
    reaction_dir.mkdir(parents=True)
    finished = make_queue_entry(
        queue_id="q-finished",
        reaction_dir=reaction_dir,
        status=QueueStatus.FAILED,
        enqueued_at="2026-09-01T00:00:00+00:00",
        started_at="2026-09-01T00:01:00+00:00",
        finished_at="2026-09-01T01:00:00+00:00",
    )
    resubmitted = make_queue_entry(
        queue_id="q-resubmitted",
        reaction_dir=reaction_dir,
        enqueued_at="2026-09-26T00:00:00+00:00",
    )
    queue_persistence.save_entries(allowed, [finished, resubmitted])
    monkeypatch.chdir(allowed / "batch")
    target = {
        "absolute": str(reaction_dir),
        "relative": "batch/water",
        "basename": "water",
        "working-directory": "./water",
        "trailing-slash": f"{reaction_dir}/",
    }[alias]

    payload, _error = activity.cancel_activity(target=target, runs_root=allowed)

    # Older terminal generations keep the directory aliases; the one active
    # generation is the cancellable target.
    assert payload["activity_id"] == "q-resubmitted"
    assert payload["status"] == "cancelled"
    assert {entry.queue_id: entry.status for entry in list_queue(allowed)} == {
        "q-finished": QueueStatus.FAILED,
        "q-resubmitted": QueueStatus.CANCELLED,
    }

    # A retry with no active generation observes the newest terminal outcome.
    retry, error = activity.cancel_activity(target=target, runs_root=allowed)
    assert error == ""
    assert retry["activity_id"] == "q-resubmitted"
    assert retry["status"] == "cancelled"


def test_target_rows_select_active_then_newest_terminal(tmp_path: Path) -> None:
    def record(
        activity_id: str, status: str, updated_at: str, *, directory: str = "a/water"
    ) -> activity.ActivityRecord:
        return activity.ActivityRecord(
            activity_id,
            "job",
            "orca",
            status,
            "water",
            ORCA_AUTO_ORCA_SOURCE,
            updated_at,
            updated_at,
            activity_id,
            aliases=("water",),
            metadata={"reaction_dir": str(tmp_path / directory)},
        )

    old = record("q-old", "failed", "2026-09-01T00:00:00+00:00")
    newer = record("q-newer", "cancelled", "2026-09-02T00:00:00+00:00")
    pending = record("q-pending", "pending", "2026-08-01T00:00:00+00:00")
    running = record("q-running", "running", "2026-08-02T00:00:00+00:00")
    elsewhere = record("q-elsewhere", "failed", "2026-09-03T00:00:00+00:00", directory="b/water")
    unlocated = activity.ActivityRecord(
        "q-unlocated",
        "job",
        "orca",
        "failed",
        "water",
        ORCA_AUTO_ORCA_SOURCE,
        "",
        "",
        "q-unlocated",
        aliases=("water",),
    )

    def named(records: list[activity.ActivityRecord]) -> list[str]:
        rows = _activity_cancel.target_rows([_row(record) for record in records], "water")
        return sorted(record.activity_id for _entry, record in rows)

    assert named([old, pending, newer]) == ["q-pending"]
    assert named([newer, old]) == ["q-newer"]
    assert named([old]) == ["q-old"]
    assert named([unlocated]) == ["q-unlocated"]
    # Several results are an ambiguity: two active generations, a shared alias
    # across directories, or a directory that cannot be verified.
    assert named([old, pending, running, newer]) == ["q-pending", "q-running"]
    assert named([old, pending, elsewhere]) == ["q-elsewhere", "q-old", "q-pending"]
    assert named([newer, elsewhere]) == ["q-elsewhere", "q-newer"]
    assert named([old, unlocated]) == ["q-old", "q-unlocated"]


@pytest.mark.parametrize("finished_status", [QueueStatus.FAILED, QueueStatus.CANCELLED])
def test_cancel_activity_basename_across_directories_stays_ambiguous(
    allowed: Path, orca_config: str, finished_status: QueueStatus
) -> None:
    project_a = allowed / "projA" / "water"
    project_b = allowed / "projB" / "water"
    project_a.mkdir(parents=True)
    project_b.mkdir(parents=True)
    finished = make_queue_entry(
        queue_id="q-b",
        reaction_dir=project_b,
        status=finished_status,
        enqueued_at="2026-09-26T00:00:00+00:00",
        started_at="2026-09-26T00:01:00+00:00",
        finished_at="2026-09-26T01:00:00+00:00",
    )
    other = make_queue_entry(
        queue_id="q-a",
        reaction_dir=project_a,
        enqueued_at="2026-09-01T00:00:00+00:00",
    )
    queue_persistence.save_entries(allowed, [finished, other])

    payload, error = activity.cancel_activity(target="water", runs_root=allowed)

    assert error == "Ambiguous activity target: water. Matches: q-a, q-b"
    assert payload["activity_id"] == ""
    assert payload["result"]["reason"] == "ambiguous"

    assert {entry.queue_id: entry.status for entry in list_queue(allowed)} == {
        "q-b": finished_status,
        "q-a": QueueStatus.PENDING,
    }
    assert not any(entry.cancel_requested for entry in list_queue(allowed))


@pytest.mark.parametrize(
    "current_generation", [True, False], ids=["current-generation", "prior-generation"]
)
def test_cancel_activity_by_state_run_id_of_running_job(
    allowed: Path, orca_config: str, current_generation: bool
) -> None:
    reaction_dir = allowed / "water"
    entry = make_queue_entry(
        queue_id="q-running",
        task_id="task-running",
        reaction_dir=reaction_dir,
        status=QueueStatus.RUNNING,
        enqueued_at="2026-09-26T00:00:00+00:00",
        started_at="2026-09-26T00:01:00+00:00",
    )
    state = write_run_state(
        reaction_dir, status=RunStatus.RUNNING, run_id="run-live", job_id=entry.task_id
    )
    state["queue_id"] = entry.queue_id
    state["queue_generation"] = (
        queue_entry_generation_token(entry) if current_generation else "q-previous"
    )
    save_state(reaction_dir, state)
    queue_persistence.save_entries(allowed, [entry])

    with acquire_run_lock(reaction_dir):
        [row] = [row for row in _records(allowed) if row.activity_id == entry.queue_id]
        # Only the state of this queue generation lends its run ID as an alias.
        assert ("run-live" in row.aliases) is current_generation
        if not current_generation:
            return
        payload, error = activity.cancel_activity(target="run-live", runs_root=allowed)

    assert error == ""
    assert payload["activity_id"] == entry.queue_id
    assert payload["cancel_target"] == entry.queue_id
    assert payload["result"]["queue_id"] == entry.queue_id
    assert payload["result"]["status"] == "cancel_requested"
    [persisted] = list_queue(allowed)
    assert persisted.cancel_requested


def test_queue_list_autodiscovers_the_config_when_no_args(
    allowed: Path,
    orca_config: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(ORCA_AUTO_CONFIG_ENV_VAR, orca_config)

    assert cli_main(["queue", "list", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 0
    assert payload["activities"] == []
    assert payload["sources"] == {"orca_config": str(Path(orca_config).resolve())}
    # Listing reads the queue and states in place; it keeps no projection.
    assert sorted(path.name for path in allowed.iterdir()) == ["queue.lock"]


def test_list_orders_mixed_timezones_and_pages_several_statuses(
    allowed: Path, orca_config: str
) -> None:
    def finished(number: int, status: QueueStatus, finished_at: str) -> QueueEntry:
        return make_queue_entry(
            queue_id=f"q-{number}",
            reaction_dir=allowed / f"job-{number}",
            status=status,
            enqueued_at="2026-01-01T00:00:00Z",
            finished_at=finished_at,
        )

    queue_persistence.save_entries(
        allowed,
        [
            finished(0, QueueStatus.COMPLETED, "2026-01-01T11:00:00+09:00"),
            finished(1, QueueStatus.FAILED, "2026-01-01T00:00:00-04:00"),
            finished(2, QueueStatus.COMPLETED, "bad-timestamp"),
            finished(3, QueueStatus.CANCELLED, "2026-01-01T03:00:00Z"),
            finished(4, QueueStatus.COMPLETED, "2026-01-01T01:00:00Z"),
        ],
    )

    payload = activity.list_activities(
        config_path=orca_config, runs_root=allowed, limit=3, statuses=[" Failed", "completed"]
    )

    # 04:00Z, 02:00Z, 01:00Z; the unparsable time sorts last and falls off the page.
    assert [row["activity_id"] for row in payload["activities"]] == ["q-1", "q-0", "q-4"]
    assert payload["count"] == 3


def test_filtered_page_keeps_catalog_wide_blockers_and_active_count(
    allowed: Path, orca_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orca_auto.activity import _list
    from orca_auto.core.queue.publication import (
        QUEUE_RECORD_SYNC_BLOCKED_KEY,
        QUEUE_RECORD_SYNC_KEY,
        QUEUE_RECORD_SYNC_REPAIR_PENDING,
    )

    blocked = make_queue_entry(
        queue_id="q-blocked",
        reaction_dir=allowed / "blocked",
        metadata={
            QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_REPAIR_PENDING,
            QUEUE_RECORD_SYNC_BLOCKED_KEY: {
                "reason": "index unavailable",
                "scope": "orca_queue",
                "next_action": "Restore index access.",
            },
        },
    )
    live = make_queue_entry(
        queue_id="q-live", reaction_dir=allowed / "live", status=QueueStatus.RUNNING
    )
    done = make_queue_entry(
        queue_id="q-done", reaction_dir=allowed / "done", status=QueueStatus.COMPLETED
    )
    queue_persistence.save_entries(allowed, [blocked, live, done])
    (allowed / "live").mkdir()
    # Without a readable admission store the listing's own count is reported.
    monkeypatch.setattr(
        _list, "global_active_simulations", lambda runs_root, *, fallback: (fallback, None)
    )

    with acquire_run_lock(allowed / "live"):
        payload = activity.list_activities(
            config_path=orca_config, runs_root=allowed, statuses=["completed"], limit=1
        )

    assert [row["activity_id"] for row in payload["activities"]] == ["q-done"]
    assert payload["active_simulations"] == 1
    assert payload["admission_blockers"] == [
        {
            "queue_id": "q-blocked",
            "allowed_root": str(allowed),
            "scope": "orca_queue",
            "reason": "index unavailable",
            "next_action": "Restore index access.",
        }
    ]
