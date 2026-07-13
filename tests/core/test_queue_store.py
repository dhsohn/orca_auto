from __future__ import annotations

import json
import os
import signal
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from itertools import count
from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from orca_auto.core.queue import publication, store
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
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
        app_name="app",
        task_id="task",
        task_kind="kind",
        engine="engine",
        status=QueueStatus.RUNNING,
        enqueued_at="2026-04-19T00:00:00+00:00",
        metadata={"reaction_key": "current", "candidate_count": 0},
    )
    token = queue_entry_generation_token(entry)

    lifecycle_update = replace(
        entry,
        status=QueueStatus.CANCELLED,
        cancel_requested=True,
        error="cancel_requested",
        metadata={
            "reaction_key": "current",
            "candidate_count": 3,
            QUEUE_RECORD_SYNC_UPDATED_AT_KEY: "later",
        },
    )
    replacement = replace(entry, metadata={"reaction_key": "replacement"})

    assert queue_entry_generation_token(lifecycle_update) == token
    assert queue_entry_generation_token(replacement) != token


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
        "metadata": metadata or {},
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


@pytest.mark.parametrize("priority", [False, 1.0, 1.5, "1.5"])
def test_enqueue_entry_rejects_noninteger_priority_before_persistence(
    tmp_path: Path,
    priority: object,
) -> None:
    entry = store.QueueEntry(
        queue_id="q-invalid-priority",
        app_name="app",
        task_id="task",
        task_kind="kind",
        engine="engine",
        priority=priority,  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="priority must be an integer"):
        store.enqueue_entry(tmp_path, entry)

    assert not _queue_file(tmp_path).exists()


@pytest.mark.parametrize("priority", [0, -7])
def test_enqueue_entry_preserves_zero_and_negative_priority(
    tmp_path: Path,
    priority: int,
) -> None:
    entry = store.QueueEntry(
        queue_id=f"q-priority-{priority}",
        app_name="app",
        task_id=f"task-{priority}",
        task_kind="kind",
        engine="engine",
        priority=priority,
    )

    persisted = store.enqueue_entry(tmp_path, entry)

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
    with pytest.raises(store.QueueStoreCorruptError, match="priority must be an integer"):
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
    entries = store.list_queue(tmp_path)
    assert len(entries) == 1
    assert entries[0].status == QueueStatus.PENDING
    assert entries[0].metadata == {}


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


def test_enqueue_entry_supports_key_duplicate_policy_with_force(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    reaction_dir = str(tmp_path / "rxn")

    def reaction_key(entry: store.QueueEntry) -> str:
        return str(entry.metadata.get("reaction_dir", ""))

    def reject_duplicate_reaction(
        entries: Sequence[store.QueueEntry],
        entry: store.QueueEntry,
    ) -> None:
        store.reject_duplicate_entry_key(
            entries,
            key=reaction_key(entry),
            key_fn=reaction_key,
            force=bool(entry.metadata.get("force", False)),
        )

    def entry(queue_id: str, status: QueueStatus, *, force: bool = False) -> store.QueueEntry:
        return store.QueueEntry(
            queue_id=queue_id,
            app_name="app",
            task_id=f"task-{queue_id}",
            task_kind="kind",
            engine="engine",
            status=status,
            metadata={"reaction_dir": reaction_dir, "force": force},
        )

    store.enqueue_entry(
        tmp_path,
        entry("q-terminal", QueueStatus.COMPLETED),
        duplicate_policy=reject_duplicate_reaction,
    )

    with pytest.raises(store.DuplicateQueueEntryError):
        store.enqueue_entry(
            tmp_path,
            entry("q-unforced", QueueStatus.PENDING),
            duplicate_policy=reject_duplicate_reaction,
        )

    forced = store.enqueue_entry(
        tmp_path,
        entry("q-forced", QueueStatus.PENDING, force=True),
        duplicate_policy=reject_duplicate_reaction,
    )
    assert forced.queue_id == "q-forced"

    with pytest.raises(store.DuplicateQueueEntryError):
        store.enqueue_entry(
            tmp_path,
            entry("q-active-forced", QueueStatus.PENDING, force=True),
            duplicate_policy=reject_duplicate_reaction,
        )


def test_dequeue_next_respects_priority_time_and_insertion_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
    assert [entry.queue_id for entry in picked if entry is not None] == ["q-3", "q-4", "q-2", "q-1"]
    assert store.dequeue_next(tmp_path) is None


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


@pytest.mark.parametrize(
    "sync_state",
    [QUEUE_RECORD_SYNC_PREPARING, QUEUE_RECORD_SYNC_REPAIRING],
)
def test_dequeue_skips_live_queue_record_publishers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sync_state: str,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    blocked = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="blocked",
        task_kind="kind",
        engine="engine",
        priority=1,
        metadata={
            QUEUE_RECORD_SYNC_KEY: sync_state,
            QUEUE_RECORD_SYNC_OWNER_PID_KEY: os.getpid(),
            QUEUE_RECORD_SYNC_OWNER_START_KEY: current_process_start_token(),
            # Even a wildly old lease cannot fence a matching live process.
            QUEUE_RECORD_SYNC_UPDATED_AT_KEY: "2000-01-01T00:00:00+00:00",
        },
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


def test_dequeue_recovers_preparing_entry_after_publisher_is_sigkilled(
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

    with publication.queue_record_publication_lock(
        tmp_path,
        queue_id,
        timeout_seconds=1,
    ):
        pass

    claimed = store.dequeue_next(tmp_path)

    assert claimed is not None
    assert claimed.queue_id == queue_id


def test_dequeue_recovers_when_publisher_pid_was_reused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    monkeypatch.setattr(publication, "_owner_process_alive", lambda _pid: True)
    monkeypatch.setattr(publication, "process_start_token", lambda _pid: "new-process")
    entry = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="pid-reused",
        task_kind="kind",
        engine="engine",
        metadata={
            QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_PREPARING,
            QUEUE_RECORD_SYNC_OWNER_PID_KEY: 1234,
            QUEUE_RECORD_SYNC_OWNER_START_KEY: "original-publisher",
            QUEUE_RECORD_SYNC_UPDATED_AT_KEY: datetime.now(UTC).isoformat(),
        },
    )

    claimed = store.dequeue_next(tmp_path)

    assert claimed is not None
    assert claimed.queue_id == entry.queue_id


def test_dequeue_recovers_publication_owned_by_zombie_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    owner_pid = 1234
    monkeypatch.setattr(publication.os, "kill", lambda _pid, _signal: None)
    original_read_text = Path.read_text

    def read_proc_stat(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if path == Path(f"/proc/{owner_pid}/stat"):
            return f"{owner_pid} (publisher) Z 1 1 1"
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_proc_stat)
    entry = store.enqueue(
        tmp_path,
        app_name="app",
        task_id="zombie-publisher",
        task_kind="kind",
        engine="engine",
        metadata={
            QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_PREPARING,
            QUEUE_RECORD_SYNC_OWNER_PID_KEY: owner_pid,
            QUEUE_RECORD_SYNC_OWNER_START_KEY: "same-process",
            QUEUE_RECORD_SYNC_UPDATED_AT_KEY: datetime.now(UTC).isoformat(),
        },
    )

    claimed = store.dequeue_next(tmp_path)

    assert claimed is not None
    assert claimed.queue_id == entry.queue_id


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
    assert updated.metadata == {"keep": "yes", "sync": "complete", "added": 1}
    assert store.update_metadata(tmp_path, "missing", {"sync": "complete"}) is None


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

    updated = store.requeue_running_entry(tmp_path, running.queue_id)
    assert updated is not None
    assert updated.status == QueueStatus.PENDING
    assert updated.started_at == ""
    assert updated.cancel_requested is False
    assert updated.error == ""
    assert updated.metadata == {"keep": "yes"}

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

    updated = store.requeue_running_entry(tmp_path, running.queue_id)
    assert updated is not None
    assert updated.status == QueueStatus.CANCELLED
    assert updated.finished_at != ""
    # The cancel has been honored; the terminal entry no longer advertises it.
    assert updated.cancel_requested is False

    # The cancelled entry is terminal and is never handed back out for a resume.
    assert store.dequeue_next(tmp_path) is None


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
    assert updated.metadata == {"keep": "yes", "shared": "new", "added": 42}
    if helper_name != "mark_completed":
        assert updated.error == str(helper_kwargs["error"]).strip()
    assert helper(tmp_path, "missing-queue-id", **helper_kwargs) is None


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
