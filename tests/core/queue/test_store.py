from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path

import pytest

from orca_auto.core.queue import publication, store
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.queue.worker.admission import select_next_claimable_entry
from tests.queue_store_helpers import (
    _claim_next,
    _enqueue,
    _entry,
    _install_deterministic_helpers,
    _queue_file,
    _without_sync_metadata,
)


def test_entry_to_dict_serializes_status_value() -> None:
    entry = store.QueueEntry(
        queue_id="q-1",
        app_name="app",
        task_id="task",
        task_kind="kind",
        engine="engine",
        status=QueueStatus.RUNNING,
        priority=5,
        enqueued_at="2026-04-19T00:00:00+00:00",
        started_at="2026-04-19T00:00:01+00:00",
    )

    serialized = store.entry_to_dict(entry)

    assert serialized["status"] == "running"
    assert serialized["queue_id"] == "q-1"


def test_list_queue_handles_missing_and_rejects_corrupt_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)

    assert store.list_queue(tmp_path) == []

    _queue_file(tmp_path).write_text("{not valid json", encoding="utf-8")
    with pytest.raises(store.QueueStoreCorruptError):
        store.list_queue(tmp_path)

    _queue_file(tmp_path).write_text(json.dumps({"queue_id": "q-1"}), encoding="utf-8")
    with pytest.raises(store.QueueStoreCorruptError):
        store.list_queue(tmp_path)

    _queue_file(tmp_path).write_text(
        json.dumps([{**_entry("q-2"), "status": "not-a-real-status"}], indent=2),
        encoding="utf-8",
    )
    with pytest.raises(store.QueueStoreCorruptError, match="Unknown queue status"):
        store.list_queue(tmp_path)

    _queue_file(tmp_path).write_text(
        json.dumps([{**_entry("q-bad-priority"), "priority": False}], indent=2),
        encoding="utf-8",
    )
    with pytest.raises(store.QueueStoreCorruptError, match="priority.*must be an integer"):
        store.list_queue(tmp_path)

    _queue_file(tmp_path).write_text(
        json.dumps([_entry("q-3"), "not-a-dict"], indent=2), encoding="utf-8"
    )
    with pytest.raises(store.QueueStoreCorruptError, match="must be a JSON object"):
        store.list_queue(tmp_path)

    _queue_file(tmp_path).write_text(
        json.dumps([{**_entry("q-4"), "metadata": ["not", "a", "dict"]}], indent=2),
        encoding="utf-8",
    )
    with pytest.raises(store.QueueStoreCorruptError, match="metadata.*must be a JSON object"):
        store.list_queue(tmp_path)


@pytest.mark.parametrize(
    "rows",
    [
        [{**_entry("q-blank"), "queue_id": ""}],
        [_entry("q-duplicate"), _entry("q-duplicate", task_id="other")],
    ],
)
def test_list_queue_rejects_blank_or_duplicate_queue_ids(
    tmp_path: Path,
    rows: list[dict[str, object]],
) -> None:
    _queue_file(tmp_path).write_text(json.dumps(rows, indent=2), encoding="utf-8")

    with pytest.raises(store.QueueStoreCorruptError, match="queue_id"):
        store.list_queue(tmp_path)


def test_mutate_entries_compensates_row_when_post_commit_contract_rejects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    stages: list[str] = []
    guard_error = RuntimeError("publication target moved")
    row = store.entry_from_dict(_entry("q-1", task_id="task-1"))

    def append(entries: list[QueueEntry]) -> tuple[QueueEntry, bool]:
        stages.append("before")
        entries.append(row)
        return row, True

    def reject_after_commit() -> None:
        stages.append("after")
        raise guard_error

    with pytest.raises(store.QueueAfterCommitError, match="publication target moved") as error_info:
        store.mutate_entries(tmp_path, append, after_commit_fn=reject_after_commit)

    assert stages == ["before", "after"]
    assert error_info.value.after_commit_error is guard_error
    assert error_info.value.compensation_outcome == "restored"
    assert error_info.value.compensation_succeeded is True
    assert error_info.value.compensation_error is None
    assert store.list_queue(tmp_path) == []


def test_after_commit_error_reports_unrestored_compensation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    persisted: list[str] = ["original"]
    save_count = 0
    guard_error = RuntimeError("publication target moved")
    rollback_error = OSError("compensation write failed before replace")

    def load(_root: Path) -> list[str]:
        return list(persisted)

    def save(_root: Path, entries: Sequence[str]) -> None:
        nonlocal save_count
        save_count += 1
        if save_count == 2:
            raise rollback_error
        persisted[:] = entries

    def append(entries: list[str]) -> tuple[str, bool]:
        entries.append("provisional")
        return "provisional-result", True

    def reject_after_commit() -> None:
        raise guard_error

    with pytest.raises(store.QueueAfterCommitError) as error_info:
        store.mutate_entries(
            tmp_path,
            append,
            after_commit_fn=reject_after_commit,
            load_entries_fn=load,
            save_entries_fn=save,
        )

    error = error_info.value
    assert error.after_commit_error is guard_error
    assert error.compensation_outcome == "not_restored"
    assert error.compensation_succeeded is False
    assert error.compensation_error is rollback_error
    assert error.verification_error is None
    assert error.provisional_result == "provisional-result"
    assert persisted == ["original", "provisional"]


def test_after_commit_error_reports_unknown_compensation_when_reload_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    persisted: list[str] = []
    load_count = 0
    save_count = 0
    verification_error = OSError("queue reload failed")

    def load(_root: Path) -> list[str]:
        nonlocal load_count
        load_count += 1
        if load_count == 2:
            raise verification_error
        return list(persisted)

    def save(_root: Path, entries: Sequence[str]) -> None:
        nonlocal save_count
        save_count += 1
        if save_count == 2:
            raise OSError("compensation write failed")
        persisted[:] = entries

    def append(entries: list[str]) -> tuple[str, bool]:
        entries.append("provisional")
        return "provisional-result", True

    def reject_after_commit() -> None:
        raise RuntimeError("publication target moved")

    with pytest.raises(store.QueueAfterCommitError) as error_info:
        store.mutate_entries(
            tmp_path,
            append,
            after_commit_fn=reject_after_commit,
            load_entries_fn=load,
            save_entries_fn=save,
        )

    error = error_info.value
    assert error.compensation_outcome == "unknown"
    assert error.compensation_succeeded is False
    assert error.verification_error is verification_error
    assert persisted == ["provisional"]


def test_after_commit_error_requires_clean_return_to_report_restored_compensation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    persisted: list[str] = ["original"]
    save_count = 0
    rollback_error = OSError("compensation fsync failed after replace")

    def load(_root: Path) -> list[str]:
        return list(persisted)

    def save(_root: Path, entries: Sequence[str]) -> None:
        nonlocal save_count
        save_count += 1
        persisted[:] = entries
        if save_count == 2:
            raise rollback_error

    def append(entries: list[str]) -> tuple[str, bool]:
        entries.append("provisional")
        return "provisional-result", True

    def reject_after_commit() -> None:
        raise RuntimeError("publication target moved")

    with pytest.raises(store.QueueAfterCommitError) as error_info:
        store.mutate_entries(
            tmp_path,
            append,
            after_commit_fn=reject_after_commit,
            load_entries_fn=load,
            save_entries_fn=save,
        )

    error = error_info.value
    assert error.compensation_outcome == "unknown"
    assert error.compensation_succeeded is False
    assert error.compensation_error is rollback_error
    assert error.verification_error is None
    assert persisted == ["original"]


@pytest.mark.parametrize(
    "guard_error",
    [
        pytest.param(KeyboardInterrupt("interrupted after commit"), id="keyboard-interrupt"),
        pytest.param(SystemExit("exiting after commit"), id="system-exit"),
    ],
)
def test_after_commit_base_exception_is_wrapped_after_clean_compensation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    guard_error: BaseException,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    persisted: list[str] = []

    def load(_root: Path) -> list[str]:
        return list(persisted)

    def save(_root: Path, entries: Sequence[str]) -> None:
        persisted[:] = entries

    def append(entries: list[str]) -> tuple[str, bool]:
        entries.append("provisional")
        return "provisional-result", True

    def reject_after_commit() -> None:
        raise guard_error

    with pytest.raises(store.QueueAfterCommitError) as error_info:
        store.mutate_entries(
            tmp_path,
            append,
            after_commit_fn=reject_after_commit,
            load_entries_fn=load,
            save_entries_fn=save,
        )

    error = error_info.value
    assert error.after_commit_error is guard_error
    assert error.compensation_outcome == "restored"
    assert error.compensation_succeeded is True
    assert error.compensation_error is None
    assert persisted == []


@pytest.mark.parametrize(
    "compensation_error",
    [
        pytest.param(
            KeyboardInterrupt("interrupted before compensation replace"),
            id="keyboard-interrupt",
        ),
        pytest.param(SystemExit("exiting before compensation replace"), id="system-exit"),
    ],
)
def test_compensation_base_exception_is_preserved_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    compensation_error: BaseException,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    persisted: list[str] = []
    save_count = 0
    guard_error = RuntimeError("publication target moved")

    def load(_root: Path) -> list[str]:
        return list(persisted)

    def save(_root: Path, entries: Sequence[str]) -> None:
        nonlocal save_count
        save_count += 1
        if save_count == 2:
            raise compensation_error
        persisted[:] = entries

    def append(entries: list[str]) -> tuple[str, bool]:
        entries.append("provisional")
        return "provisional-result", True

    with pytest.raises(store.QueueAfterCommitError) as error_info:
        store.mutate_entries(
            tmp_path,
            append,
            after_commit_fn=lambda: (_ for _ in ()).throw(guard_error),
            load_entries_fn=load,
            save_entries_fn=save,
        )

    error = error_info.value
    assert error.after_commit_error is guard_error
    assert error.compensation_outcome == "not_restored"
    assert error.compensation_succeeded is False
    assert error.compensation_error is compensation_error
    assert error.provisional_result == "provisional-result"
    assert persisted == ["provisional"]


@pytest.mark.parametrize(
    "verification_error",
    [
        pytest.param(KeyboardInterrupt("queue reload interrupted"), id="keyboard-interrupt"),
        pytest.param(SystemExit("queue reload exited"), id="system-exit"),
    ],
)
def test_compensation_verification_base_exception_is_preserved_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verification_error: BaseException,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    persisted: list[str] = []
    load_count = 0
    save_count = 0

    def load(_root: Path) -> list[str]:
        nonlocal load_count
        load_count += 1
        if load_count == 2:
            raise verification_error
        return list(persisted)

    def save(_root: Path, entries: Sequence[str]) -> None:
        nonlocal save_count
        save_count += 1
        if save_count == 2:
            raise OSError("compensation write failed")
        persisted[:] = entries

    def append(entries: list[str]) -> tuple[str, bool]:
        entries.append("provisional")
        return "provisional-result", True

    with pytest.raises(store.QueueAfterCommitError) as error_info:
        store.mutate_entries(
            tmp_path,
            append,
            after_commit_fn=lambda: (_ for _ in ()).throw(RuntimeError("publication target moved")),
            load_entries_fn=load,
            save_entries_fn=save,
        )

    error = error_info.value
    assert error.compensation_outcome == "unknown"
    assert error.compensation_succeeded is False
    assert isinstance(error.compensation_error, OSError)
    assert error.verification_error is verification_error
    assert persisted == ["provisional"]


def test_reject_duplicate_entry_key_supports_force_over_terminal_only(
    tmp_path: Path,
) -> None:
    reaction_dir = str(tmp_path / "rxn")

    def reaction_key(entry: store.QueueEntry) -> str:
        return str(entry.metadata.get("reaction_dir", ""))

    def entry(queue_id: str, status: QueueStatus) -> store.QueueEntry:
        return store.QueueEntry(
            queue_id=queue_id,
            app_name="app",
            task_id=f"task-{queue_id}",
            task_kind="kind",
            engine="engine",
            status=status,
            metadata={"reaction_dir": reaction_dir},
        )

    terminal_only = [entry("q-terminal", QueueStatus.COMPLETED)]

    with pytest.raises(store.DuplicateQueueEntryError):
        store.reject_duplicate_entry_key(
            terminal_only,
            key=reaction_dir,
            key_fn=reaction_key,
            force=False,
        )

    store.reject_duplicate_entry_key(
        terminal_only,
        key=reaction_dir,
        key_fn=reaction_key,
        force=True,
    )

    with_active = [*terminal_only, entry("q-active", QueueStatus.PENDING)]

    with pytest.raises(store.DuplicateQueueEntryError):
        store.reject_duplicate_entry_key(
            with_active,
            key=reaction_dir,
            key_fn=reaction_key,
            force=True,
        )


def test_claim_next_respects_priority_then_arrival_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Row position is the arrival order (rows are only appended under the
    # queue lock). Within one priority class the file order decides dispatch;
    # the wall-clock enqueued_at must not, because a stepped clock (WSL2 skew
    # correction) can stamp a later arrival with an earlier time.
    _install_deterministic_helpers(monkeypatch)

    _queue_file(tmp_path).write_text(
        json.dumps(
            [
                _entry("q-1", task_id="a", priority=3, enqueued_at="2026-04-19T00:00:03+00:00"),
                _entry("q-2", task_id="b", priority=1, enqueued_at="2026-04-19T00:00:05+00:00"),
                _entry("q-3", task_id="c", priority=1, enqueued_at="2026-04-19T00:00:01+00:00"),
                _entry("q-4", task_id="d", priority=1, enqueued_at="2026-04-19T00:00:01+00:00"),
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    picked = [_claim_next(tmp_path) for _ in range(4)]
    assert [entry.queue_id for entry in picked if entry is not None] == ["q-2", "q-3", "q-4", "q-1"]
    assert _claim_next(tmp_path) is None


def test_claim_next_keeps_fifo_when_the_clock_steps_backwards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Regression: a WSL2 skew correction between two enqueues stamped the
    # first arrival ~3s later than the second, and the old enqueued_at sort
    # key dispatched the second arrival first
    # (tests/test_queue_worker.py::TestFillSlots flake, 2026-07-16).
    monkeypatch.setattr(store, "file_lock", lambda *_args, **_kwargs: nullcontext())
    stamps = iter(
        [
            "2026-07-16T14:18:22.500000+00:00",  # first enqueue, skewed ahead
            "2026-07-16T14:18:19.400000+00:00",  # second enqueue, corrected clock
        ]
    )
    monkeypatch.setattr(store, "now_utc_iso", lambda: next(stamps, "2026-07-16T14:18:25+00:00"))

    first = _enqueue(tmp_path, app_name="app", task_id="a", task_kind="kind", engine="e")
    second = _enqueue(tmp_path, app_name="app", task_id="b", task_kind="kind", engine="e")
    assert first.enqueued_at > second.enqueued_at

    picked = _claim_next(tmp_path)
    assert picked is not None and picked.queue_id == first.queue_id


def test_select_next_claimable_entry_accept_entry_fn_skips_other_engine_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Workers for another app share the single runs root with standalone ORCA
    # jobs; the app filter must skip an ORCA entry at the atomic pop so the
    # other worker never claims (and mis-runs) it, even on the single-root path.
    _install_deterministic_helpers(monkeypatch)
    _queue_file(tmp_path).write_text(
        json.dumps(
            [
                _entry("q-orca", app_name="orca_auto_orca", priority=1),
                _entry("q-other", app_name="orca_auto_other", priority=9),
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    def accept_other(entry: object) -> bool:
        return getattr(entry, "app_name", "") in ("", "orca_auto_other")

    # Skips the higher-priority ORCA entry, selects the other app's one.
    selected = select_next_claimable_entry(store.list_queue(tmp_path), accept_entry_fn=accept_other)
    assert selected is not None and selected.queue_id == "q-other"
    claimed = store.dequeue_entry_if_pending(tmp_path, selected.queue_id)
    assert claimed is not None and claimed.queue_id == "q-other"

    # Only the ORCA entry remains; the other app's filter now selects nothing.
    assert (
        select_next_claimable_entry(store.list_queue(tmp_path), accept_entry_fn=accept_other)
        is None
    )

    # An unfiltered (ORCA) worker still selects it.
    unfiltered = select_next_claimable_entry(store.list_queue(tmp_path))
    assert unfiltered is not None and unfiltered.queue_id == "q-orca"


def test_dequeue_entry_if_pending_only_runs_selected_pending_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    _queue_file(tmp_path).write_text(
        json.dumps(
            [
                _entry("q-1", task_id="a", priority=1, enqueued_at="2026-04-19T00:00:01+00:00"),
                _entry("q-2", task_id="b", priority=9, enqueued_at="2026-04-19T00:00:02+00:00"),
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    picked = store.dequeue_entry_if_pending(tmp_path, "q-2")

    assert picked is not None
    assert picked.queue_id == "q-2"
    assert picked.status == QueueStatus.RUNNING
    entries = store.list_queue(tmp_path)
    assert [(entry.queue_id, entry.status) for entry in entries] == [
        ("q-1", QueueStatus.PENDING),
        ("q-2", QueueStatus.RUNNING),
    ]
    assert store.dequeue_entry_if_pending(tmp_path, "q-1-missing") is None
    assert store.dequeue_entry_if_pending(tmp_path, "q-2") is None


def test_dequeue_entry_if_pending_ignores_cancel_requested_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    _queue_file(tmp_path).write_text(
        json.dumps([_entry("q-cancel", cancel_requested=True)], indent=2),
        encoding="utf-8",
    )

    assert store.dequeue_entry_if_pending(tmp_path, "q-cancel") is None
    entries = store.list_queue(tmp_path)
    assert entries[0].status == QueueStatus.PENDING
    assert entries[0].cancel_requested is True


def test_update_metadata_merges_without_changing_lifecycle_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = _enqueue(
        tmp_path,
        app_name="app",
        task_id="task",
        task_kind="kind",
        engine="engine",
        metadata={"keep": "yes", "sync": "pending"},
    )

    updated = store.update_metadata(
        tmp_path,
        entry.queue_id,
        {"sync": "complete", "added": 1},
    )

    assert updated is not None
    assert updated.status == entry.status
    assert updated.enqueued_at == entry.enqueued_at
    assert _without_sync_metadata(updated.metadata) == {
        "keep": "yes",
        "sync": "complete",
        "added": 1,
    }
    assert store.update_metadata(tmp_path, "missing", {"sync": "complete"}) is None


def test_clear_terminal_removes_terminal_entries_and_can_keep_latest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)

    _queue_file(tmp_path).write_text(
        json.dumps(
            [
                _entry(
                    "q-running", status=QueueStatus.RUNNING, started_at="2026-04-19T00:00:01+00:00"
                ),
                _entry(
                    "q-done-old",
                    status=QueueStatus.COMPLETED,
                    finished_at="2026-04-19T00:00:02+00:00",
                ),
                _entry(
                    "q-cancel-mid",
                    status=QueueStatus.CANCELLED,
                    finished_at="2026-04-19T00:00:03+00:00",
                ),
                _entry(
                    "q-failed-new",
                    status=QueueStatus.FAILED,
                    finished_at="2026-04-19T00:00:04+00:00",
                ),
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    assert store.clear_terminal(tmp_path, keep_last=1) == 2
    remaining = json.loads(_queue_file(tmp_path).read_text(encoding="utf-8"))
    assert [item["queue_id"] for item in remaining] == ["q-running", "q-failed-new"]

    assert store.clear_terminal(tmp_path) == 1
    remaining = json.loads(_queue_file(tmp_path).read_text(encoding="utf-8"))
    assert [item["queue_id"] for item in remaining] == ["q-running"]

    assert store.clear_terminal(tmp_path) == 0
    assert store.clear_terminal(tmp_path / "missing") == 0


def test_clear_terminal_unlinks_publication_lock_files_of_removed_rows_only(
    tmp_path: Path,
) -> None:
    _queue_file(tmp_path).write_text(
        json.dumps(
            [
                _entry("q-running", status=QueueStatus.RUNNING),
                _entry("q-done", status=QueueStatus.COMPLETED),
            ]
        ),
        encoding="utf-8",
    )
    for queue_id in ("q-running", "q-done"):
        with publication.queue_record_publication_lock(tmp_path, queue_id):
            pass
    running_lock = publication.queue_record_publication_lock_path(tmp_path, "q-running")
    done_lock = publication.queue_record_publication_lock_path(tmp_path, "q-done")
    assert running_lock.is_file() and done_lock.is_file()

    assert store.clear_terminal(tmp_path) == 1

    assert running_lock.is_file()
    assert not done_lock.exists()
    assert store.clear_terminal(tmp_path) == 0
    assert running_lock.is_file()


def test_clear_terminal_scopes_keep_last_to_selected_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    _queue_file(tmp_path).write_text(
        json.dumps(
            [
                _entry(
                    "q-owned-old",
                    app_name="owned",
                    status=QueueStatus.COMPLETED,
                    finished_at="2026-04-19T00:00:01+00:00",
                ),
                _entry(
                    "q-foreign-new",
                    app_name="foreign",
                    status=QueueStatus.COMPLETED,
                    finished_at="2026-04-19T00:00:03+00:00",
                ),
                _entry(
                    "q-owned-new",
                    app_name="owned",
                    status=QueueStatus.COMPLETED,
                    finished_at="2026-04-19T00:00:02+00:00",
                ),
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    assert (
        store.clear_terminal(
            tmp_path,
            keep_last=1,
            select_entry_fn=lambda entry: entry.app_name == "owned",
        )
        == 1
    )
    remaining = store.list_queue(tmp_path)
    assert [entry.queue_id for entry in remaining] == ["q-foreign-new", "q-owned-new"]
