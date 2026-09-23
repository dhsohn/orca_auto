from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from orca_auto.activity import _orca, _orca_index, list_activities
from orca_auto.core import activity_index as index
from orca_auto.core import activity_invalidation as journal
from orca_auto.core.activity import ActivityListRequest, ActivitySourceRequest, sort_key
from orca_auto.core.indexing import JobLocationRecord, upsert_job_location
from orca_auto.core.indexing import store as locations
from orca_auto.core.queue import persistence as queue
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca import run_snapshot, state


def _config(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "runs"
    root.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(f"runs_root: {root}\n")
    return root, str(config)


def _entry(root: Path, number: int) -> QueueEntry:
    return QueueEntry(
        queue_id=f"q-{number:06d}",
        app_name="orca_auto_orca",
        task_id=f"task-{number}",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.COMPLETED,
        enqueued_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T01:00:00Z",
        metadata={"reaction_dir": str(root / f"job-{number}"), "run_id": f"run-{number}"},
    )


def _state(root: Path, path: Path, run_id: str = "run", status: str = "completed") -> dict:
    payload = {
        "run_id": run_id,
        "status": status,
        "started_at": "2026-01-01T00:00:00Z",
        "attempts": [],
    }
    state.save_state(path, payload)
    upsert_job_location(
        root,
        JobLocationRecord(
            job_id=run_id,
            app_name="orca_auto_orca",
            job_type="orca_sp",
            status=status,
            original_run_dir=str(path),
            latest_known_path=str(path),
        ),
    )
    return payload


def _query(root: Path, *, limit: int = 0, statuses: tuple[str, ...] = ()) -> list[dict]:
    request = ActivityListRequest(
        ActivitySourceRequest(), indexed=True, limit=limit, statuses=statuses
    )
    rows = sorted(_orca_index.query_records(root, request), key=sort_key, reverse=True)
    rows = [row for row in rows if not statuses or row.status in statuses]
    return [row.to_dict() for row in (rows[:limit] if limit else rows)]


def test_warm_limited_query_does_not_read_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = _config(tmp_path)
    queue.save_entries(root, [_entry(root, number) for number in range(10000)])
    expected = [f"q-{number:06d}" for number in range(9999, 9979, -1)]
    assert [row["activity_id"] for row in _query(root, limit=20)] == expected

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("warm limited query read historical source/state files")

    monkeypatch.setattr(queue, "load_entries", forbidden)
    monkeypatch.setattr(locations, "_load_records", forbidden)
    monkeypatch.setattr(run_snapshot, "_load_pinned_state", forbidden)
    # Also bound SQLite work: no full table scan/sort hidden behind fewer JSON reads.
    original_connect = index.connect
    steps = []

    def measured_connect(path: Path) -> sqlite3.Connection:
        connection = original_connect(path)

        def progress() -> int:
            steps.append(1)
            return 0

        connection.set_progress_handler(progress, 100)
        return connection

    monkeypatch.setattr(index, "connect", measured_connect)
    result = list_activities(orca_config=config, limit=20, statuses=("completed", "failed"))
    assert [row["activity_id"] for row in result["activities"]] == expected
    assert len(steps) < 100, f"SQLite scanned history: {len(steps) * 100} VM steps"


@pytest.mark.parametrize("at_root", [False, True])
def test_terminal_state_changes_update_only_changed_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, at_root: bool
) -> None:
    root, config = _config(tmp_path)
    target = root if at_root else root / "nested" / "target"
    payload = _state(root, target)
    for number in range(5):
        _state(root, root / f"other-{number}", f"other-{number}")
    _query(root)
    payload["status"] = "failed"
    state.save_state(target, payload)
    reads = []
    load = run_snapshot._load_pinned_state

    def counted(fd: int):
        reads.append(fd)
        return load(fd)

    monkeypatch.setattr(run_snapshot, "_load_pinned_state", counted)
    assert _query(root, limit=1, statuses=("failed",))[0]["activity_id"] == "run"
    assert len(reads) == 1
    assert _query(root) == [
        row.to_dict()
        for row in sorted(_orca.orca_records(config_path=config), key=sort_key, reverse=True)
    ]


def test_source_writer_mirror_and_missed_publication_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _config_path = _config(tmp_path)
    entries = [_entry(root, number) for number in range(30)]
    queue.save_entries(root, entries)
    _query(root)
    entries[0] = replace(entries[0], status=QueueStatus.FAILED)
    queue.save_entries(root, entries)
    original_load = queue.load_entries
    monkeypatch.setattr(
        queue, "load_entries", lambda *_a, **_k: pytest.fail("writer mirror was not used")
    )
    assert _query(root, statuses=("failed",))[0]["activity_id"] == entries[0].queue_id
    monkeypatch.setattr(queue, "load_entries", original_load)
    # Simulate an old writer or crash after canonical JSON and before projection.
    monkeypatch.setattr(queue, "published_source", lambda *_a: None)
    entries[1] = replace(entries[1], status=QueueStatus.CANCELLED)
    queue.save_entries(root, entries)
    assert _query(root, statuses=("cancelled",))[0]["activity_id"] == entries[1].queue_id


def test_timezones_and_multiple_statuses_match_canonical_order(tmp_path: Path) -> None:
    root, config = _config(tmp_path)
    entries = [_entry(root, number) for number in range(5)]
    entries[0] = replace(entries[0], finished_at="2026-01-01T11:00:00+09:00")
    entries[1] = replace(
        entries[1], finished_at="2026-01-01T00:00:00-04:00", status=QueueStatus.FAILED
    )
    entries[2] = replace(entries[2], finished_at="bad-timestamp")
    queue.save_entries(root, entries)
    expected = [
        row.to_dict()
        for row in sorted(_orca.orca_records(config_path=config), key=sort_key, reverse=True)
    ]
    assert _query(root, limit=3, statuses=("completed", "failed")) == expected[:3]


def test_lock_changes_are_refreshed_before_filter_and_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _config_path = _config(tmp_path)
    _state(root, root / "orphan", status="running")
    held = True
    monkeypatch.setattr(_orca, "run_lock_is_held", lambda *_a, **_k: held)
    assert _query(root, limit=1, statuses=("running",))[0]["status"] == "running"
    held = False
    assert _query(root, limit=1, statuses=("failed",))[0]["status"] == "failed"
    assert not _query(root, limit=1, statuses=("running",))


def test_prewrite_ticket_cannot_be_consumed_before_state_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _config_path = _config(tmp_path)
    job = root / "job"
    payload = _state(root, job)
    _query(root)
    invalidated, release, captured = Event(), Event(), Event()
    invalidate = journal.invalidate_state
    capture = journal.capture

    def pause(path: Path, *, root: Path | None = None) -> None:
        invalidate(path, root=root)
        invalidated.set()
        assert release.wait(5)

    def observe(path: Path):
        value = capture(path)
        if value:
            captured.set()
        return value

    monkeypatch.setattr(journal, "invalidate_state", pause)
    monkeypatch.setattr(journal, "capture", observe)
    payload["status"] = "failed"
    with ThreadPoolExecutor(2) as pool:
        writer = pool.submit(state.save_state, job, payload)
        assert invalidated.wait(5)
        reader = pool.submit(_query, root)
        try:
            assert captured.wait(5)
            assert not reader.done()
        finally:
            release.set()
        writer.result(timeout=5)
        assert reader.result(timeout=5)[0]["status"] == "failed"
    assert not journal.capture(root)


def test_refresh_discoveries_are_not_retained_and_missing_database_rebuilds(tmp_path: Path) -> None:
    root, config = _config(tmp_path)
    _state(root, root / "tracked")
    state.save_state(
        root / "untracked", {"run_id": "untracked", "status": "completed", "attempts": []}
    )
    assert {
        row["activity_id"]
        for row in list_activities(orca_config=config, refresh=True)["activities"]
    } == {"run", "untracked"}
    assert [row["activity_id"] for row in _query(root)] == ["run"]
    (root / index.DB_NAME).unlink()
    assert [row["activity_id"] for row in _query(root)] == ["run"]


def test_corrupt_and_wrong_root_projection_fail_visibly(tmp_path: Path) -> None:
    root, _config_path = _config(tmp_path)
    _query(root)
    with closing(index.connect(root)) as connection, connection:
        connection.execute("UPDATE meta SET value='another-root' WHERE key='root'")
    with pytest.raises(index.ActivityIndexError, match="another root"):
        _query(root)
    (root / index.DB_NAME).write_bytes(b"corrupt")
    with pytest.raises(index.ActivityIndexError, match="disposable database"):
        _query(root)


def test_journal_acknowledges_only_the_observed_revision(tmp_path: Path) -> None:
    root, _config_path = _config(tmp_path)
    _query(root)
    job = root / "not-yet-created"
    journal.invalidate_state(job)
    before = journal.capture(root)
    journal.invalidate_state(job)
    journal.acknowledge(root, before)
    after = journal.capture(root)
    assert after and after != before
    journal.acknowledge(root, after)
    assert not journal.capture(root)


def test_live_rerun_poll_does_not_rematerialize_shared_path_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _config_path = _config(tmp_path)
    job = root / "reused"
    _state(root, job, "latest", "running")
    entries = [
        replace(
            _entry(root, number), metadata={"reaction_dir": str(job), "run_id": f"old-{number}"}
        )
        for number in range(2000)
    ]
    live = replace(
        _entry(root, 2000),
        status=QueueStatus.RUNNING,
        metadata={"reaction_dir": str(job), "run_id": "latest"},
    )
    queue.save_entries(root, [*entries, live])
    held = True
    monkeypatch.setattr(_orca, "run_lock_is_held", lambda *_a, **_k: held)
    assert _query(root, statuses=("running",))[0]["activity_id"] == live.queue_id
    monkeypatch.setattr(
        run_snapshot,
        "_load_pinned_state",
        lambda *_a: pytest.fail("lock poll reread historical state"),
    )
    calls = []
    original = _orca.queue_record

    def record(*args, **kwargs):
        calls.append(args[1].queue_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(_orca, "queue_record", record)
    held = False
    assert _query(root, limit=1, statuses=("pending",))[0]["activity_id"] == live.queue_id
    assert calls == [live.queue_id]


def test_new_ticket_during_projection_commit_is_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _config_path = _config(tmp_path)
    job = root / "job"
    payload = _state(root, job)
    _query(root)
    payload["status"] = "failed"
    state.save_state(job, payload)
    acknowledge = journal.acknowledge
    injected = False

    def interleave(path: Path, tickets):
        nonlocal injected
        if not injected:
            injected = True
            payload["status"] = "cancelled"
            state.save_state(job, payload)
        acknowledge(path, tickets)

    monkeypatch.setattr(journal, "acknowledge", interleave)
    assert _query(root)[0]["status"] == "cancelled"
    assert not journal.capture(root)


def test_source_change_during_state_read_retries_current_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _config_path = _config(tmp_path)
    job = root / "job"
    _state(root, job)
    entry = replace(_entry(root, 0), metadata={"reaction_dir": str(job), "run_id": "run"})
    queue.save_entries(root, [entry])
    load = run_snapshot._load_pinned_state
    injected = False

    def interleave(fd: int):
        nonlocal injected
        if not injected:
            injected = True
            queue.save_entries(root, [replace(entry, status=QueueStatus.FAILED)])
        return load(fd)

    monkeypatch.setattr(run_snapshot, "_load_pinned_state", interleave)
    assert _query(root)[0]["status"] == "failed"


def test_refresh_repairs_out_of_band_state_edit(tmp_path: Path) -> None:
    root, config = _config(tmp_path)
    job = root / "job"
    _state(root, job)
    _query(root)
    path = job / "job_state.json"
    payload = json.loads(path.read_text())
    payload["status"]["state"] = "failed"
    path.write_text(json.dumps(payload))
    expected = [
        row.to_dict()
        for row in sorted(_orca.orca_records(config_path=config), key=sort_key, reverse=True)
    ]
    assert expected[0]["status"] == "failed"
    assert list_activities(orca_config=config, refresh=True)["activities"] == expected


def test_normalized_metadata_links_follow_state_updates(tmp_path: Path) -> None:
    root, config = _config(tmp_path)
    job = root / "job"
    payload = _state(root, job)
    entry = replace(
        _entry(root, 0),
        status=QueueStatus.RUNNING,
        metadata={"reaction_dir": f" {job} ", "run_id": " run "},
    )
    queue.save_entries(root, [entry])
    _query(root)
    payload["status"] = "failed"
    state.save_state(job, payload)
    actual = _query(root)
    expected = [
        row.to_dict()
        for row in sorted(_orca.orca_records(config_path=config), key=sort_key, reverse=True)
    ]
    assert actual == expected
    assert actual[0]["status"] == "failed"


def test_invalid_cache_never_turns_successful_canonical_write_into_failure(tmp_path: Path) -> None:
    root, _config_path = _config(tmp_path)
    unrelated = root.parent / "other.db"
    unrelated.write_text("keep")
    (root / index.DB_NAME).symlink_to(unrelated)
    entry = _entry(root, 0)
    queue.save_entries(root, [entry])
    assert queue.load_entries(root) == [entry]
    assert unrelated.read_text() == "keep"
    with pytest.raises(index.ActivityIndexError, match="single-link regular"):
        _query(root)


def test_projection_probe_failure_preserves_successful_queue_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _config_path = _config(tmp_path)
    original = Path.is_file

    def inaccessible(path: Path) -> bool:
        if path == root / index.DB_NAME:
            raise PermissionError("cache probe denied")
        return original(path)

    monkeypatch.setattr(Path, "is_file", inaccessible)
    entry = _entry(root, 0)
    queue.save_entries(root, [entry])
    assert queue.load_entries(root) == [entry]
