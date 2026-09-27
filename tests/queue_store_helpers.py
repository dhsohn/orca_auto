"""Queue-file builders shared by the core queue store, transition and publication tests.

Each writes or reads ``queue.json`` under a test root through the real
``orca_auto.core.queue`` store; ``_install_deterministic_helpers`` swaps the
store's file lock and clock for deterministic stand-ins.
"""

from __future__ import annotations

from contextlib import nullcontext
from itertools import count
from pathlib import Path

import pytest

from orca_auto.core.queue import publication, store, transitions
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_BLOCKED_KEY,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_OWNER_START_KEY,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
)
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.queue.worker.admission import select_next_claimable_entry
from orca_auto.core.utils.persistence import timestamped_token


def _install_deterministic_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    time_counter = count(1)

    monkeypatch.setattr(store, "file_lock", lambda *_args, **_kwargs: nullcontext())

    def clock() -> str:
        return f"2026-04-19T00:00:{next(time_counter):02d}+00:00"

    monkeypatch.setattr(store, "now_utc_iso", clock)
    monkeypatch.setattr(transitions, "now_utc_iso", clock)


def _claim_next(root: Path) -> QueueEntry | None:
    """Preview the head of ``root`` and claim it by id, as a worker does."""
    entry = select_next_claimable_entry(store.list_queue(root))
    if entry is None:
        return None
    return store.dequeue_entry_if_pending(root, entry.queue_id, expected_entry=entry)


def _queue_file(root: Path) -> Path:
    return root / "queue.json"


def _enqueue(
    root: str | Path,
    *,
    app_name: str,
    task_id: str,
    task_kind: str,
    engine: str,
    priority: int = 10,
    metadata: dict[str, object] | None = None,
) -> QueueEntry:
    """Append one pending row the way the submission adapter persists it."""
    queue_id = timestamped_token("q")
    row_metadata = dict(metadata or {})
    if QUEUE_RECORD_SYNC_KEY not in row_metadata:
        row_metadata.update(
            publication.queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_COMPLETE,
                token=queue_id,
                owner_pid=0,
            )
        )
    entry = QueueEntry(
        queue_id=queue_id,
        app_name=app_name,
        task_id=task_id,
        task_kind=task_kind,
        engine=engine,
        priority=priority,
        enqueued_at=store.now_utc_iso(),
        metadata=row_metadata,
    )

    def append(entries: list[QueueEntry]) -> tuple[QueueEntry, bool]:
        entries.append(entry)
        return entry, True

    return store.mutate_entries(root, append)


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
            QUEUE_RECORD_SYNC_BLOCKED_KEY,
        }
    }
