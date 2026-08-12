from __future__ import annotations

import json
import os
import signal
from collections.abc import Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from itertools import count
from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from orca_auto.core.queue import publication, store
from orca_auto.core.queue.enqueue_publication import repair_enqueue_publication
from orca_auto.core.queue.generation import (
    queue_entries_same_generation,
    queue_entry_generation_token,
)
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_OWNER_START_KEY,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_REPAIRING,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
    current_process_start_token,
)
from orca_auto.core.queue.types import QueueStatus


def _install_deterministic_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    token_counter = count(1)
    time_counter = count(1)

    monkeypatch.setattr(store, "file_lock", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(
        store, "timestamped_token", lambda prefix: f"{prefix}_{next(token_counter):04d}"
    )
    monkeypatch.setattr(
        store,
        "now_utc_iso",
        lambda: f"2026-04-19T00:00:{next(time_counter):02d}+00:00",
    )


def test_queue_generation_token_tracks_only_immutable_identity() -> None:
    entry = store.QueueEntry(
        queue_id="q-generation",
        app_name="orca_auto_orca",
        task_id="orca-task",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.PENDING,
        enqueued_at="2026-04-19T00:00:00+00:00",
        metadata={
            "job_dir": "/runs/water-md",
            "resource_request": {"max_cores": 2, "max_memory_gb": 4},
            "resource_actual": {"max_cores": 2, "max_memory_gb": 4},
            "execution_snapshot": {
                "version": 2,
                "generation_name": "20260419-000000-a1b2c3d4",
                "execution_dir": "/runs/water-md/20260419-000000-a1b2c3d4",
                "execution_dir_identity": {"device": 11, "inode": 22},
                "selected_input_xyz": "/runs/water-md/20260419-000000-a1b2c3d4/input.xyz",
            },
            "retry_supported": False,
            "resume_supported": False,
        },
    )
    token = queue_entry_generation_token(entry)

    running = replace(
        entry,
        status=QueueStatus.RUNNING,
        started_at="2026-04-19T00:01:00+00:00",
        metadata={
            **entry.metadata,
            "execution_dir": "/runs/water-md/20260419-000000-a1b2c3d4",
            "attempt": 1,
            "run_id": "run_20260419_runtime",
            "orca_terminal_replay": None,
            "orca_terminal_replay_fence_only": True,
        },
    )
    terminal = replace(
        running,
        status=QueueStatus.CANCELLED,
        finished_at="2026-04-19T00:02:00+00:00",
        cancel_requested=True,
        error="cancel_requested",
        metadata={
            **running.metadata,
            "terminal_artifacts": {
                "trajectory": {"path": "xtb.trj", "sha256": "a" * 64},
            },
            QUEUE_RECORD_SYNC_UPDATED_AT_KEY: "later",
        },
    )

    assert queue_entry_generation_token(running) == token
    assert queue_entry_generation_token(terminal) == token
    assert queue_entries_same_generation(running, entry)
    assert queue_entries_same_generation(terminal, entry)


def test_queue_generation_rejects_immutable_metadata_changes() -> None:
    entry = store.QueueEntry(
        queue_id="q-generation",
        app_name="orca_auto_orca",
        task_id="orca-task",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.PENDING,
        enqueued_at="2026-04-19T00:00:00+00:00",
        metadata={
            "job_dir": "/runs/water-md",
            "resource_request": {"max_cores": 2, "max_memory_gb": 4},
            "resource_actual": {"max_cores": 2, "max_memory_gb": 4},
            "execution_snapshot": {
                "version": 2,
                "generation_name": "20260419-000000-a1b2c3d4",
                "execution_dir": "/runs/water-md/20260419-000000-a1b2c3d4",
                "execution_dir_identity": {"device": 11, "inode": 22},
                "selected_input_xyz": "/runs/water-md/20260419-000000-a1b2c3d4/input.xyz",
            },
            "retry_supported": False,
            "resume_supported": False,
        },
    )
    token = queue_entry_generation_token(entry)
    replacements = (
        replace(entry, metadata={**entry.metadata, "job_dir": "/runs/replacement"}),
        replace(
            entry,
            metadata={
                **entry.metadata,
                "resource_request": {"max_cores": 4, "max_memory_gb": 4},
            },
        ),
        replace(
            entry,
            metadata={
                **entry.metadata,
                "resource_actual": {"max_cores": 4, "max_memory_gb": 4},
            },
        ),
        replace(
            entry,
            metadata={
                **entry.metadata,
                "execution_snapshot": {
                    "version": 2,
                    "generation_name": "20260419-000001-deadbeef",
                    "execution_dir": "/runs/water-md/20260419-000001-deadbeef",
                    "execution_dir_identity": {"device": 11, "inode": 33},
                    "selected_input_xyz": "/runs/water-md/20260419-000001-deadbeef/input.xyz",
                },
            },
        ),
        replace(entry, metadata={**entry.metadata, "retry_supported": True}),
        replace(entry, metadata={**entry.metadata, "resume_supported": True}),
    )

    for replacement in replacements:
        assert queue_entry_generation_token(replacement) != token
        assert not queue_entries_same_generation(replacement, entry)


def _queue_file(root: Path) -> Path:
    return root / "queue.json"


def _enqueue_transient_publisher_then_crash(
    queue_root: str,
    ready_connection: Connection,
) -> None:
    entry = store.enqueue(
        queue_root,
        app_name="app",
        task_id="crashed-publisher",
        task_kind="kind",
        engine="engine",
        metadata={
            QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_PREPARING,
            QUEUE_RECORD_SYNC_OWNER_PID_KEY: os.getpid(),
            QUEUE_RECORD_SYNC_OWNER_START_KEY: current_process_start_token(),
            QUEUE_RECORD_SYNC_UPDATED_AT_KEY: datetime.now(UTC).isoformat(),
        },
    )
    # Die while owning the same process-scoped lock used around publication.
    # The kernel must release it so cancellation/repair can recover the entry.
    with publication.queue_record_publication_lock(queue_root, entry.queue_id):
        ready_connection.send(entry.queue_id)
        os.kill(os.getpid(), signal.SIGKILL)


def _entry(
    queue_id: str,
    *,
    app_name: str = "app",
    task_id: str = "task",
    task_kind: str = "kind",
    engine: str = "engine",
    status: QueueStatus = QueueStatus.PENDING,
    priority: int = 10,
    enqueued_at: str = "2026-04-19T00:00:00+00:00",
    started_at: str = "",
    finished_at: str = "",
    cancel_requested: bool = False,
    error: str = "",
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    entry_metadata = {
        **publication.queue_record_sync_metadata(
            publication.QUEUE_RECORD_SYNC_COMPLETE,
            token=queue_id,
            owner_pid=0,
        ),
        **(metadata or {}),
    }
    return {
        "queue_id": queue_id,
        "app_name": app_name,
        "task_id": task_id,
        "task_kind": task_kind,
        "engine": engine,
        "status": status.value,
        "priority": priority,
        "enqueued_at": enqueued_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "cancel_requested": cancel_requested,
        "error": error,
        "metadata": entry_metadata,
    }


def _without_sync_metadata(metadata: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in metadata.items()
        if key
        not in {
            QUEUE_RECORD_SYNC_KEY,
            QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
            QUEUE_RECORD_SYNC_OWNER_PID_KEY,
            QUEUE_RECORD_SYNC_OWNER_START_KEY,
            QUEUE_RECORD_SYNC_TOKEN_KEY,
        }
    }


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


@pytest.mark.parametrize("priority", [False, 1.0, 1.5, "1.5"])
def test_enqueue_rejects_noninteger_priority_before_persistence(
    tmp_path: Path,
    priority: object,
) -> None:
    with pytest.raises(ValueError, match="priority must be an integer"):
        store.enqueue(
            tmp_path,
            app_name="app",
            task_id="task",
            task_kind="kind",
            engine="engine",
            priority=priority,  # type: ignore[arg-type]
        )

    assert not _queue_file(tmp_path).exists()


@pytest.mark.parametrize("priority", [0, -7])
def test_enqueue_preserves_zero_and_negative_priority(
    tmp_path: Path,
    priority: int,
) -> None:
    persisted = store.enqueue(
        tmp_path,
        app_name="app",
        task_id=f"task-{priority}",
        task_kind="kind",
        engine="engine",
        priority=priority,
    )

    assert persisted.priority == priority
    assert store.list_queue(tmp_path)[0].priority == priority


def test_enqueue_retries_queue_id_collision_under_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = iter(["q_same", "q_same", "q_unique"])
    monkeypatch.setattr(store, "timestamped_token", lambda _prefix: next(generated))

    first = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="task-first",
        task_kind="kind",
        engine="engine",
    )
    second = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="task-second",
        task_kind="kind",
        engine="engine",
    )

    assert first.queue_id == "q_same"
    assert second.queue_id == "q_unique"
    assert [entry.queue_id for entry in store.list_queue(tmp_path)] == ["q_same", "q_unique"]


def test_enqueue_permanent_queue_id_collision_preserves_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store, "timestamped_token", lambda _prefix: "q_same")
    store.enqueue(
        tmp_path,
        app_name="app",
        task_id="task-first",
        task_kind="kind",
        engine="engine",
    )
    queue_path = _queue_file(tmp_path)
    original = queue_path.read_bytes()

    with pytest.raises(RuntimeError, match="unique queue id"):
        store.enqueue(
            tmp_path,
            app_name="app",
            task_id="task-second",
            task_kind="kind",
            engine="engine",
        )

    assert queue_path.read_bytes() == original
    [remaining] = store.list_queue(tmp_path)
    assert remaining.task_id == "task-first"


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


def test_queue_store_facade_groups_root_and_overrides(tmp_path: Path) -> None:
    entries = [
        store.QueueEntry(
            queue_id="q-1",
            app_name="app",
            task_id="task",
            task_kind="kind",
            engine="engine",
        )
    ]
    saved: list[tuple[Path, Sequence[store.QueueEntry]]] = []
    queue_store = store.QueueStore.for_root(
        tmp_path,
        load_entries_fn=lambda root: entries,
        save_entries_fn=lambda root, updated: saved.append((root, list(updated))),
    )

    def mark_running(
        entry: store.QueueEntry,
    ) -> tuple[store.QueueEntry, store.QueueEntry]:
        updated = store.QueueEntry(
            queue_id=entry.queue_id,
            app_name=entry.app_name,
            task_id=entry.task_id,
            task_kind=entry.task_kind,
            engine=entry.engine,
            status=QueueStatus.RUNNING,
        )
        return updated, updated

    assert queue_store.list_entries() == entries
    updated = queue_store.mutate_entry_by_id("q-1", mark_running, missing_result=None)

    assert updated is not None
    assert updated.status == QueueStatus.RUNNING
    assert entries[0].status == QueueStatus.RUNNING
    assert saved == [(tmp_path.resolve(), entries)]


def test_enqueue_rejects_corrupt_queue_file_without_overwriting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    corrupt_text = "{not valid json"
    _queue_file(tmp_path).write_text(corrupt_text, encoding="utf-8")

    with pytest.raises(store.QueueStoreCorruptError):
        store.enqueue(
            tmp_path,
            app_name="app",
            task_id="task-1",
            task_kind="kind",
            engine="engine",
        )

    assert _queue_file(tmp_path).read_text(encoding="utf-8") == corrupt_text


def test_enqueue_compensates_row_when_post_commit_contract_rejects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    stages: list[str] = []
    guard_error = RuntimeError("publication target moved")

    def before_commit() -> None:
        stages.append("before")

    def reject_after_commit() -> None:
        stages.append("after")
        raise guard_error

    with pytest.raises(store.QueueAfterCommitError, match="publication target moved") as error_info:
        store.enqueue(
            tmp_path,
            app_name="app",
            task_id="task-1",
            task_kind="kind",
            engine="engine",
            before_commit_fn=before_commit,
            after_commit_fn=reject_after_commit,
        )

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

    queue_store = store.QueueStore.for_root(
        tmp_path,
        load_entries_fn=load,
        save_entries_fn=save,
    )

    def append(entries: list[str]) -> tuple[str, bool]:
        entries.append("provisional")
        return "provisional-result", True

    def reject_after_commit() -> None:
        raise guard_error

    with pytest.raises(store.QueueAfterCommitError) as error_info:
        queue_store.mutate_entries(
            append,
            after_commit_fn=reject_after_commit,
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

    queue_store = store.QueueStore.for_root(
        tmp_path,
        load_entries_fn=load,
        save_entries_fn=save,
    )

    def append(entries: list[str]) -> tuple[str, bool]:
        entries.append("provisional")
        return "provisional-result", True

    def reject_after_commit() -> None:
        raise RuntimeError("publication target moved")

    with pytest.raises(store.QueueAfterCommitError) as error_info:
        queue_store.mutate_entries(append, after_commit_fn=reject_after_commit)

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

    queue_store = store.QueueStore.for_root(
        tmp_path,
        load_entries_fn=load,
        save_entries_fn=save,
    )

    def append(entries: list[str]) -> tuple[str, bool]:
        entries.append("provisional")
        return "provisional-result", True

    def reject_after_commit() -> None:
        raise RuntimeError("publication target moved")

    with pytest.raises(store.QueueAfterCommitError) as error_info:
        queue_store.mutate_entries(append, after_commit_fn=reject_after_commit)

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

    queue_store = store.QueueStore.for_root(
        tmp_path,
        load_entries_fn=load,
        save_entries_fn=save,
    )

    def append(entries: list[str]) -> tuple[str, bool]:
        entries.append("provisional")
        return "provisional-result", True

    def reject_after_commit() -> None:
        raise guard_error

    with pytest.raises(store.QueueAfterCommitError) as error_info:
        queue_store.mutate_entries(append, after_commit_fn=reject_after_commit)

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

    queue_store = store.QueueStore.for_root(
        tmp_path,
        load_entries_fn=load,
        save_entries_fn=save,
    )

    def append(entries: list[str]) -> tuple[str, bool]:
        entries.append("provisional")
        return "provisional-result", True

    with pytest.raises(store.QueueAfterCommitError) as error_info:
        queue_store.mutate_entries(
            append,
            after_commit_fn=lambda: (_ for _ in ()).throw(guard_error),
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

    queue_store = store.QueueStore.for_root(
        tmp_path,
        load_entries_fn=load,
        save_entries_fn=save,
    )

    def append(entries: list[str]) -> tuple[str, bool]:
        entries.append("provisional")
        return "provisional-result", True

    with pytest.raises(store.QueueAfterCommitError) as error_info:
        queue_store.mutate_entries(
            append,
            after_commit_fn=lambda: (_ for _ in ()).throw(RuntimeError("publication target moved")),
        )

    error = error_info.value
    assert error.compensation_outcome == "unknown"
    assert error.compensation_succeeded is False
    assert isinstance(error.compensation_error, OSError)
    assert error.verification_error is verification_error
    assert persisted == ["provisional"]


def test_enqueue_blocks_active_duplicates_and_allows_reenqueue_after_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)

    first = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="task-1",
        task_kind="kind",
        engine="engine",
    )

    running = store.dequeue_next(tmp_path)
    assert running is not None
    assert running.queue_id == first.queue_id
    assert running.status == QueueStatus.RUNNING

    with pytest.raises(store.DuplicateQueueEntryError):
        store.enqueue(
            tmp_path,
            app_name="app",
            task_id="task-1",
            task_kind="kind",
            engine="engine",
        )

    completed = store.mark_completed(tmp_path, first.queue_id)
    assert completed is not None
    assert completed.status == QueueStatus.COMPLETED

    second = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="task-1",
        task_kind="kind",
        engine="engine",
    )

    entries = store.list_queue(tmp_path)
    assert [entry.status for entry in entries] == [QueueStatus.COMPLETED, QueueStatus.PENDING]
    assert second.queue_id != first.queue_id


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


def test_dequeue_next_respects_priority_then_arrival_order(
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

    picked = [store.dequeue_next(tmp_path) for _ in range(4)]
    assert [entry.queue_id for entry in picked if entry is not None] == ["q-2", "q-3", "q-4", "q-1"]
    assert store.dequeue_next(tmp_path) is None


def test_dequeue_next_keeps_fifo_when_the_clock_steps_backwards(
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

    first = store.enqueue(tmp_path, app_name="app", task_id="a", task_kind="kind", engine="e")
    second = store.enqueue(tmp_path, app_name="app", task_id="b", task_kind="kind", engine="e")
    assert first.enqueued_at > second.enqueued_at

    picked = store.dequeue_next(tmp_path)
    assert picked is not None and picked.queue_id == first.queue_id


def test_dequeue_next_accept_entry_fn_skips_other_engine_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Internal-engine workers share the single runs root with standalone ORCA
    # jobs; the app filter must skip an ORCA entry at the atomic pop so a CREST
    # or xTB worker never claims (and mis-runs) it, even on the single-root path.
    _install_deterministic_helpers(monkeypatch)
    _queue_file(tmp_path).write_text(
        json.dumps(
            [
                _entry("q-orca", app_name="orca_auto_orca", priority=1),
                _entry("q-xtb", app_name="orca_auto_xtb", priority=9),
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    def accept_xtb(entry: object) -> bool:
        return getattr(entry, "app_name", "") in ("", "orca_auto_xtb")

    # Skips the higher-priority ORCA entry, claims the xTB one.
    claimed = store.dequeue_next(tmp_path, accept_entry_fn=accept_xtb)
    assert claimed is not None and claimed.queue_id == "q-xtb"

    # Only the ORCA entry remains; the xTB filter now claims nothing.
    assert store.dequeue_next(tmp_path, accept_entry_fn=accept_xtb) is None

    # An unfiltered (ORCA) worker still claims it.
    unfiltered = store.dequeue_next(tmp_path)
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


def test_dequeue_entry_if_pending_rejects_replacement_with_same_queue_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    _queue_file(tmp_path).write_text(
        json.dumps([_entry("q-same", task_id="task-a")], indent=2),
        encoding="utf-8",
    )
    [selected] = store.list_queue(tmp_path)
    _queue_file(tmp_path).write_text(
        json.dumps([_entry("q-same", task_id="task-b")], indent=2),
        encoding="utf-8",
    )

    assert (
        store.dequeue_entry_if_pending(
            tmp_path,
            "q-same",
            expected_entry=selected,
        )
        is None
    )
    [replacement] = store.list_queue(tmp_path)
    assert replacement.task_id == "task-b"
    assert replacement.status == QueueStatus.PENDING


def test_request_cancel_rejects_replacement_with_same_queue_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    _queue_file(tmp_path).write_text(
        json.dumps([_entry("q-same", task_id="task-a")], indent=2),
        encoding="utf-8",
    )
    [selected] = store.list_queue(tmp_path)
    _queue_file(tmp_path).write_text(
        json.dumps([_entry("q-same", task_id="task-b")], indent=2),
        encoding="utf-8",
    )

    assert store.request_cancel(tmp_path, "q-same", expected_entry=selected) is None
    [replacement] = store.list_queue(tmp_path)
    assert replacement.task_id == "task-b"
    assert replacement.status == QueueStatus.PENDING
    assert replacement.cancel_requested is False


def test_request_cancel_accepts_same_generation_after_publication_transition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    preparing_metadata = {
        "job_dir": str(tmp_path / "job"),
        **publication.queue_record_sync_metadata(
            publication.QUEUE_RECORD_SYNC_PREPARING,
            token="publication-token",
            owner_pid=os.getpid(),
        ),
    }
    _queue_file(tmp_path).write_text(
        json.dumps([_entry("q-same", metadata=preparing_metadata)], indent=2),
        encoding="utf-8",
    )
    [selected] = store.list_queue(tmp_path)
    complete_metadata = {
        **preparing_metadata,
        **publication.queue_record_sync_metadata(
            publication.QUEUE_RECORD_SYNC_COMPLETE,
            token="publication-token",
            owner_pid=0,
        ),
    }
    _queue_file(tmp_path).write_text(
        json.dumps([_entry("q-same", metadata=complete_metadata)], indent=2),
        encoding="utf-8",
    )

    cancelled = store.request_cancel(tmp_path, "q-same", expected_entry=selected)

    assert cancelled is not None
    assert cancelled.status == QueueStatus.CANCELLED


def test_pending_cancel_callback_runs_before_queue_transition_under_both_locks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    lock_state = {"publication": False, "queue": False}

    @contextmanager
    def publication_lock(_root: Path, _queue_id: str):
        assert lock_state == {"publication": False, "queue": False}
        lock_state["publication"] = True
        try:
            yield
        finally:
            lock_state["publication"] = False

    @contextmanager
    def queue_mutation_lock(_root: Path, *, timeout_seconds: float = 10.0):
        del timeout_seconds
        assert lock_state == {"publication": True, "queue": False}
        lock_state["queue"] = True
        try:
            yield
        finally:
            lock_state["queue"] = False

    entry = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="pending-callback",
        task_kind="kind",
        engine="engine",
    )
    monkeypatch.setattr(store, "queue_record_publication_lock", publication_lock)
    monkeypatch.setattr(store, "queue_lock", queue_mutation_lock)
    events: list[tuple[str, QueueStatus]] = []
    real_save_entries = store.save_entries

    def pending_metadata(candidate: store.QueueEntry) -> dict[str, object]:
        assert lock_state == {"publication": True, "queue": True}
        assert candidate.status == QueueStatus.CANCELLED
        assert candidate.cancel_requested is True
        events.append(("metadata", candidate.status))
        return {"terminal_replay": {"status": candidate.status.value}}

    def before_pending_cancel(candidate: store.QueueEntry) -> None:
        assert lock_state == {"publication": True, "queue": True}
        [durable] = store.load_entries(tmp_path)
        assert durable.status == QueueStatus.PENDING
        assert candidate.status == QueueStatus.CANCELLED
        assert candidate.metadata["terminal_replay"] == {"status": "cancelled"}
        events.append(("callback", candidate.status))

    def save_after_callback(root: Path, entries: Sequence[store.QueueEntry]) -> None:
        assert lock_state == {"publication": True, "queue": True}
        events.append(("save", entries[0].status))
        real_save_entries(root, entries)

    cancelled = store.request_cancel(
        tmp_path,
        entry.queue_id,
        expected_entry=entry,
        pending_metadata_update_fn=pending_metadata,
        before_pending_cancel_fn=before_pending_cancel,
        save_entries_fn=save_after_callback,
    )

    assert cancelled is not None and cancelled.status == QueueStatus.CANCELLED
    assert events == [
        ("metadata", QueueStatus.CANCELLED),
        ("callback", QueueStatus.CANCELLED),
        ("save", QueueStatus.CANCELLED),
    ]
    assert lock_state == {"publication": False, "queue": False}


def test_pending_cancel_callback_failure_leaves_queue_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="pending-callback-failure",
        task_kind="kind",
        engine="engine",
    )

    def reject_cancel(_candidate: store.QueueEntry) -> None:
        raise OSError("artifact publication failed")

    with pytest.raises(OSError, match="artifact publication failed"):
        store.request_cancel(
            tmp_path,
            entry.queue_id,
            expected_entry=entry,
            before_pending_cancel_fn=reject_cancel,
        )

    [unchanged] = store.list_queue(tmp_path)
    assert unchanged.status == QueueStatus.PENDING
    assert unchanged.cancel_requested is False
    assert unchanged.finished_at == ""


def test_pending_cancel_metadata_callback_failure_aborts_queue_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="pending-metadata-failure",
        task_kind="kind",
        engine="engine",
        metadata={"keep": "yes"},
    )
    before = _queue_file(tmp_path).read_bytes()
    save_calls = 0

    def reject_metadata(_candidate: store.QueueEntry) -> dict[str, object]:
        raise OSError("metadata generation failed")

    def count_save(_root: Path, _entries: Sequence[store.QueueEntry]) -> None:
        nonlocal save_calls
        save_calls += 1

    with pytest.raises(OSError, match="metadata generation failed"):
        store.request_cancel(
            tmp_path,
            entry.queue_id,
            pending_metadata_update_fn=reject_metadata,
            save_entries_fn=count_save,
        )

    assert save_calls == 0
    assert _queue_file(tmp_path).read_bytes() == before
    [unchanged] = store.list_queue(tmp_path)
    assert unchanged.status == QueueStatus.PENDING
    assert _without_sync_metadata(unchanged.metadata) == {"keep": "yes"}


def test_running_cancel_does_not_invoke_pending_cancel_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    pending = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="running-callback",
        task_kind="kind",
        engine="engine",
    )
    running = store.dequeue_next(tmp_path)
    assert running is not None

    requested = store.request_cancel(
        tmp_path,
        pending.queue_id,
        expected_entry=running,
        pending_metadata_update_fn=lambda _candidate: pytest.fail(
            "pending metadata callback must not run for a running cancellation"
        ),
        before_pending_cancel_fn=lambda _candidate: pytest.fail(
            "running cancellation must remain worker-owned"
        ),
    )

    assert requested is not None
    assert requested.status == QueueStatus.RUNNING
    assert requested.cancel_requested is True


def test_terminal_mark_rejects_replacement_with_same_queue_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    _queue_file(tmp_path).write_text(
        json.dumps(
            [_entry("q-same", task_id="task-a", status=QueueStatus.RUNNING)],
            indent=2,
        ),
        encoding="utf-8",
    )
    [selected] = store.list_queue(tmp_path)
    _queue_file(tmp_path).write_text(
        json.dumps(
            [_entry("q-same", task_id="task-b", status=QueueStatus.RUNNING)],
            indent=2,
        ),
        encoding="utf-8",
    )

    assert store.mark_completed(tmp_path, "q-same", expected_entry=selected) is None
    [replacement] = store.list_queue(tmp_path)
    assert replacement.task_id == "task-b"
    assert replacement.status == QueueStatus.RUNNING


def test_terminal_completion_does_not_overwrite_acknowledged_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="task-a",
        task_kind="kind",
        engine="engine",
    )
    running = store.dequeue_next(tmp_path)
    assert running is not None
    assert store.request_cancel(tmp_path, entry.queue_id, expected_entry=running) is not None

    assert store.mark_completed(tmp_path, entry.queue_id, expected_entry=running) is None
    [cancel_requested] = store.list_queue(tmp_path)
    assert cancel_requested.status == QueueStatus.RUNNING
    assert cancel_requested.cancel_requested is True
    assert store.mark_cancelled(tmp_path, entry.queue_id, expected_entry=running) is not None
    [cancelled] = store.list_queue(tmp_path)
    assert cancelled.status == QueueStatus.CANCELLED


def test_request_cancel_handles_pending_running_and_terminal_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)

    pending = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="pending",
        task_kind="kind",
        engine="engine",
    )
    pending_cancelled = store.request_cancel(tmp_path, pending.queue_id)
    assert pending_cancelled is not None
    assert pending_cancelled.status == QueueStatus.CANCELLED
    assert pending_cancelled.cancel_requested is True
    assert pending_cancelled.finished_at == "2026-04-19T00:00:02+00:00"

    running = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="running",
        task_kind="kind",
        engine="engine",
    )
    dequeued = store.dequeue_next(tmp_path)
    assert dequeued is not None
    assert dequeued.queue_id == running.queue_id

    running_cancelled = store.request_cancel(tmp_path, running.queue_id)
    assert running_cancelled is not None
    assert running_cancelled.status == QueueStatus.RUNNING
    assert running_cancelled.cancel_requested is True
    assert running_cancelled.finished_at == ""
    assert store.get_cancel_requested(tmp_path, running.queue_id) is True
    assert store.get_cancel_requested(tmp_path, "missing-queue-id") is False
    assert store.request_cancel(tmp_path, "missing-queue-id") is None

    terminal = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="terminal",
        task_kind="kind",
        engine="engine",
    )
    assert store.mark_completed(tmp_path, terminal.queue_id) is not None
    assert store.request_cancel(tmp_path, terminal.queue_id) is None


def test_request_cancel_revalidates_selected_identity_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = store.enqueue(
        tmp_path,
        app_name="foreign-app",
        task_id="foreign-task",
        task_kind="foreign-kind",
        engine="foreign",
    )

    assert (
        store.request_cancel(
            tmp_path,
            entry.queue_id,
            accept_entry_fn=lambda current: current.engine == "owned",
        )
        is None
    )
    [unchanged] = store.list_queue(tmp_path)
    assert unchanged.status == QueueStatus.PENDING


def test_request_cancel_revokes_transient_publication_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="publishing",
        task_kind="kind",
        engine="engine",
        metadata={
            QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_PREPARING,
            QUEUE_RECORD_SYNC_OWNER_PID_KEY: os.getpid(),
            QUEUE_RECORD_SYNC_OWNER_START_KEY: current_process_start_token(),
            QUEUE_RECORD_SYNC_TOKEN_KEY: "publisher-token",
            QUEUE_RECORD_SYNC_UPDATED_AT_KEY: datetime.now(UTC).isoformat(),
        },
    )

    cancelled = store.request_cancel(tmp_path, entry.queue_id)

    assert cancelled is not None
    assert cancelled.status == QueueStatus.CANCELLED
    assert cancelled.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED
    assert cancelled.metadata[QUEUE_RECORD_SYNC_OWNER_PID_KEY] == 0
    assert cancelled.metadata[QUEUE_RECORD_SYNC_OWNER_START_KEY] == ""
    assert cancelled.metadata[QUEUE_RECORD_SYNC_TOKEN_KEY] == ""


@pytest.mark.parametrize("owner_recorded", [True, False])
@pytest.mark.parametrize(
    "sync_state",
    [QUEUE_RECORD_SYNC_PREPARING, QUEUE_RECORD_SYNC_REPAIRING],
)
def test_dequeue_skips_transient_publication_and_claims_the_next_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sync_state: str,
    owner_recorded: bool,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    metadata: dict[str, object] = {
        QUEUE_RECORD_SYNC_KEY: sync_state,
        QUEUE_RECORD_SYNC_UPDATED_AT_KEY: "2000-01-01T00:00:00+00:00",
    }
    if owner_recorded:
        # Owner metadata is recorded in half the matrix and absent in the other
        # half: an uncommitted publication is unclaimable either way. The case
        # where the recorded owner is genuinely dead is pinned separately by
        # test_sigkilled_publisher_row_stays_parked_until_repair_publishes.
        metadata[QUEUE_RECORD_SYNC_OWNER_PID_KEY] = os.getpid()
        metadata[QUEUE_RECORD_SYNC_OWNER_START_KEY] = current_process_start_token()
    blocked = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="blocked",
        task_kind="kind",
        engine="engine",
        priority=1,
        metadata=metadata,
    )
    ready = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="ready",
        task_kind="kind",
        engine="engine",
        priority=9,
    )

    assert store.dequeue_entry_if_pending(tmp_path, blocked.queue_id) is None
    claimed = store.dequeue_next(tmp_path)

    # A parked publication must not stall the rows behind it.
    assert claimed is not None
    assert claimed.queue_id == ready.queue_id


@pytest.mark.parametrize("sync_state", ["repair_pendng", QUEUE_RECORD_SYNC_ABORTED])
def test_dequeue_quarantines_unknown_or_aborted_publication_state(
    tmp_path: Path,
    sync_state: str,
) -> None:
    blocked = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="blocked",
        task_kind="kind",
        engine="engine",
        metadata={QUEUE_RECORD_SYNC_KEY: sync_state},
    )

    assert store.dequeue_entry_if_pending(tmp_path, blocked.queue_id) is None
    assert store.dequeue_next(tmp_path) is None


def test_sigkilled_publisher_row_stays_parked_until_repair_publishes(
    tmp_path: Path,
) -> None:
    ctx = get_context("fork")
    read_connection, write_connection = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=_enqueue_transient_publisher_then_crash,
        args=(str(tmp_path), write_connection),
    )
    process.start()
    write_connection.close()
    assert read_connection.poll(10)
    queue_id = read_connection.recv()
    read_connection.close()
    process.join(timeout=10)
    assert process.exitcode == -signal.SIGKILL

    # The kernel released the publication lock the dead publisher held, so
    # recovery is never blocked on the crashed process.
    with publication.queue_record_publication_lock(
        tmp_path,
        queue_id,
        timeout_seconds=1,
    ):
        pass

    # The row is still not claimable: its queued record was never published,
    # and the dead owner PID is not a licence to run the job without one.
    assert store.dequeue_next(tmp_path) is None
    [parked] = store.list_queue(tmp_path)
    assert parked.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_PREPARING

    published: list[str] = []
    assert repair_enqueue_publication(
        tmp_path,
        parked,
        publish=lambda current: published.append(current.queue_id),
        label="test",
    )
    assert published == [queue_id]

    [repaired] = store.list_queue(tmp_path)
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE

    claimed = store.dequeue_next(tmp_path)

    assert claimed is not None
    assert claimed.queue_id == queue_id


def test_update_metadata_merges_without_changing_lifecycle_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = store.enqueue(
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


def test_update_metadata_can_fence_the_exact_queue_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    submitted = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="task",
        task_kind="kind",
        engine="engine",
        metadata={"generation": "original"},
    )
    current = store.update_metadata(
        tmp_path,
        submitted.queue_id,
        {"attached": True},
        expected_entry=submitted,
        expected_task_id=submitted.task_id,
    )

    assert current is not None
    assert current.metadata["attached"] is True
    assert (
        store.update_metadata(
            tmp_path,
            submitted.queue_id,
            {"stale_writer": True},
            expected_entry=submitted,
            expected_task_id=submitted.task_id,
        )
        is None
    )
    assert (
        store.update_metadata(
            tmp_path,
            submitted.queue_id,
            {"wrong_task": True},
            expected_entry=current,
            expected_task_id="replacement-task",
        )
        is None
    )
    persisted = store.list_queue(tmp_path)[0]
    assert "stale_writer" not in persisted.metadata
    assert "wrong_task" not in persisted.metadata


def test_requeue_running_entry_returns_running_entry_to_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)

    running = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="running",
        task_kind="kind",
        engine="engine",
        metadata={"keep": "yes"},
    )
    dequeued = store.dequeue_next(tmp_path)
    assert dequeued is not None
    assert dequeued.queue_id == running.queue_id
    assert dequeued.status == QueueStatus.RUNNING

    updated = store.requeue_running_entry(
        tmp_path,
        running.queue_id,
        cancel_metadata_update_fn=lambda _candidate: pytest.fail(
            "cancel metadata callback must not run for an ordinary requeue"
        ),
    )
    assert updated is not None
    assert updated.status == QueueStatus.PENDING
    assert updated.started_at == ""
    assert updated.cancel_requested is False
    assert updated.error == ""
    assert _without_sync_metadata(updated.metadata) == {"keep": "yes"}

    entries = store.list_queue(tmp_path)
    assert len(entries) == 1
    assert entries[0].status == QueueStatus.PENDING
    assert store.requeue_running_entry(tmp_path, "missing-queue-id") is None


def test_requeue_running_entry_cancels_when_cancel_requested(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A cancel arriving while the entry runs must not be resurrected by the
    # worker-shutdown requeue path: workers deliver cancellation as a SIGTERM the
    # run treats as a shutdown requeue, which previously cleared cancel_requested
    # and let the cancelled job be dequeued and resumed.
    _install_deterministic_helpers(monkeypatch)

    running = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="running",
        task_kind="kind",
        engine="engine",
    )
    assert store.dequeue_next(tmp_path) is not None
    assert store.request_cancel(tmp_path, running.queue_id) is not None

    lock_held = False

    @contextmanager
    def mutation_lock(_root: Path, *, timeout_seconds: float = 10.0):
        nonlocal lock_held
        del timeout_seconds
        assert lock_held is False
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    monkeypatch.setattr(store, "queue_lock", mutation_lock)

    def cancel_metadata(candidate: store.QueueEntry) -> dict[str, object]:
        assert lock_held is True
        assert candidate.status == QueueStatus.CANCELLED
        assert candidate.cancel_requested is False
        return {"terminal_replay": {"status": candidate.status.value}}

    updated = store.requeue_running_entry(
        tmp_path,
        running.queue_id,
        cancel_metadata_update_fn=cancel_metadata,
    )
    assert updated is not None
    assert updated.status == QueueStatus.CANCELLED
    assert updated.finished_at != ""
    # The cancel has been honored; the terminal entry no longer advertises it.
    assert updated.cancel_requested is False
    assert updated.metadata["terminal_replay"] == {"status": "cancelled"}
    assert lock_held is False

    # The cancelled entry is terminal and is never handed back out for a resume.
    assert store.dequeue_next(tmp_path) is None


def test_requeue_cancel_metadata_callback_failure_aborts_queue_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="running-metadata-failure",
        task_kind="kind",
        engine="engine",
        metadata={"keep": "yes"},
    )
    running = store.dequeue_next(tmp_path)
    assert running is not None
    requested = store.request_cancel(tmp_path, entry.queue_id)
    assert requested is not None and requested.cancel_requested is True
    before = _queue_file(tmp_path).read_bytes()
    save_calls = 0

    def reject_metadata(_candidate: store.QueueEntry) -> dict[str, object]:
        raise OSError("metadata generation failed")

    def count_save(_root: Path, _entries: Sequence[store.QueueEntry]) -> None:
        nonlocal save_calls
        save_calls += 1

    with pytest.raises(OSError, match="metadata generation failed"):
        store.requeue_running_entry(
            tmp_path,
            entry.queue_id,
            cancel_metadata_update_fn=reject_metadata,
            save_entries_fn=count_save,
        )

    assert save_calls == 0
    assert _queue_file(tmp_path).read_bytes() == before
    [unchanged] = store.list_queue(tmp_path)
    assert unchanged.status == QueueStatus.RUNNING
    assert unchanged.cancel_requested is True
    assert _without_sync_metadata(unchanged.metadata) == {"keep": "yes"}


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


@pytest.mark.parametrize(
    ("helper_name", "helper_kwargs", "expected_status"),
    [
        ("mark_completed", {}, QueueStatus.COMPLETED),
        ("mark_failed", {"error": "  boom  "}, QueueStatus.FAILED),
        ("mark_cancelled", {"error": "  stop  "}, QueueStatus.CANCELLED),
    ],
)
def test_mark_helpers_merge_metadata_updates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    helper_name: str,
    helper_kwargs: dict[str, object],
    expected_status: QueueStatus,
) -> None:
    _install_deterministic_helpers(monkeypatch)

    entry = store.enqueue(
        tmp_path,
        app_name="app",
        task_id=f"task-{helper_name}",
        task_kind="kind",
        engine="engine",
        metadata={"keep": "yes", "shared": "old"},
    )

    helper = getattr(store, helper_name)
    updated = helper(
        tmp_path,
        entry.queue_id,
        metadata_update={"shared": "new", "added": 42},
        **helper_kwargs,
    )

    assert updated is not None
    assert updated.status == expected_status
    assert _without_sync_metadata(updated.metadata) == {
        "keep": "yes",
        "shared": "new",
        "added": 42,
    }
    if helper_name != "mark_completed":
        assert updated.error == str(helper_kwargs["error"]).strip()
    assert helper(tmp_path, "missing-queue-id", **helper_kwargs) is None


@pytest.mark.parametrize(
    ("helper_name", "helper_kwargs", "expected_status"),
    [
        ("mark_completed", {}, QueueStatus.COMPLETED),
        ("mark_failed", {"error": "boom"}, QueueStatus.FAILED),
        ("mark_cancelled", {}, QueueStatus.CANCELLED),
    ],
)
def test_mark_helpers_merge_callback_metadata_under_queue_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    helper_name: str,
    helper_kwargs: dict[str, object],
    expected_status: QueueStatus,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = store.enqueue(
        tmp_path,
        app_name="app",
        task_id=f"callback-{helper_name}",
        task_kind="kind",
        engine="engine",
        metadata={"keep": "yes", "shared": "original"},
    )
    lock_held = False

    @contextmanager
    def mutation_lock(_root: Path, *, timeout_seconds: float = 10.0):
        nonlocal lock_held
        del timeout_seconds
        assert lock_held is False
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    monkeypatch.setattr(store, "queue_lock", mutation_lock)
    callback_entries: list[store.QueueEntry] = []

    def dynamic_metadata(current: store.QueueEntry) -> dict[str, object]:
        assert lock_held is True
        assert current.status == QueueStatus.PENDING
        callback_entries.append(current)
        return {"shared": "dynamic", "terminal_replay": expected_status.value}

    helper = getattr(store, helper_name)
    updated = helper(
        tmp_path,
        entry.queue_id,
        metadata_update={"shared": "static", "static": True},
        metadata_update_fn=dynamic_metadata,
        **helper_kwargs,
    )

    assert updated is not None and updated.status == expected_status
    assert _without_sync_metadata(updated.metadata) == {
        "keep": "yes",
        "shared": "dynamic",
        "static": True,
        "terminal_replay": expected_status.value,
    }
    assert callback_entries == [entry]
    assert lock_held is False


def test_mark_metadata_callback_failure_aborts_queue_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="mark-metadata-failure",
        task_kind="kind",
        engine="engine",
        metadata={"keep": "yes"},
    )
    before = _queue_file(tmp_path).read_bytes()
    save_calls = 0

    def reject_metadata(_current: store.QueueEntry) -> dict[str, object]:
        raise OSError("metadata generation failed")

    def count_save(_root: Path, _entries: Sequence[store.QueueEntry]) -> None:
        nonlocal save_calls
        save_calls += 1

    with pytest.raises(OSError, match="metadata generation failed"):
        store.mark_failed(
            tmp_path,
            entry.queue_id,
            error="boom",
            metadata_update_fn=reject_metadata,
            save_entries_fn=count_save,
        )

    assert save_calls == 0
    assert _queue_file(tmp_path).read_bytes() == before
    [unchanged] = store.list_queue(tmp_path)
    assert unchanged.status == QueueStatus.PENDING
    assert unchanged.error == ""
    assert _without_sync_metadata(unchanged.metadata) == {"keep": "yes"}


def _write_single_running_entry(root: Path) -> None:
    _queue_file(root).write_text(
        json.dumps([_entry("q-1", status=QueueStatus.RUNNING)], indent=2),
        encoding="utf-8",
    )


def test_mark_status_never_flips_a_terminal_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cancel landing just after a natural completion (or the reverse) must
    # not rewrite the recorded result; only update_terminal reconciles.
    _install_deterministic_helpers(monkeypatch)
    _write_single_running_entry(tmp_path)

    completed = store.mark_completed(tmp_path, "q-1")
    assert completed is not None and completed.status == QueueStatus.COMPLETED

    refused = store.mark_cancelled(tmp_path, "q-1")
    assert refused is None
    assert store.list_queue(tmp_path)[0].status == QueueStatus.COMPLETED


def test_mark_status_does_not_resurrect_a_cancelled_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_deterministic_helpers(monkeypatch)
    _write_single_running_entry(tmp_path)

    cancelled = store.mark_cancelled(tmp_path, "q-1")
    assert cancelled is not None and cancelled.status == QueueStatus.CANCELLED

    refused = store.mark_completed(tmp_path, "q-1")
    assert refused is None
    assert store.list_queue(tmp_path)[0].status == QueueStatus.CANCELLED


def test_mark_status_replays_same_terminal_side_effect_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_deterministic_helpers(monkeypatch)
    _write_single_running_entry(tmp_path)
    [running] = store.list_queue(tmp_path)
    assert (
        store.mark_cancelled(
            tmp_path,
            "q-1",
            metadata_update={"candidate_count": 2},
            expected_entry=running,
        )
        is not None
    )
    calls: list[str] = []

    replayed = store.mark_cancelled(
        tmp_path,
        "q-1",
        expected_entry=running,
        require_cancel_requested=True,
        before_update_fn=lambda: calls.append("repair"),
    )

    assert replayed is not None
    assert replayed.status == QueueStatus.CANCELLED
    assert calls == ["repair"]
