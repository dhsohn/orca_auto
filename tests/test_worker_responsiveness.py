from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from orca_auto.core.queue import store
from orca_auto.core.queue.enqueue_publication import repair_enqueue_publication_outcome
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_PREPARING,
    queue_record_publication_lock,
    queue_record_sync_metadata,
    queue_record_sync_state,
)
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.utils.lock import FileLockTimeoutError


def _pending_publication(root: Path) -> QueueEntry:
    entry = QueueEntry(
        queue_id="queue",
        app_name="orca_auto_orca",
        task_id="job",
        task_kind="orca_run_inp",
        engine="orca",
        metadata=queue_record_sync_metadata(
            QUEUE_RECORD_SYNC_PREPARING, token="original-publisher", owner_pid=0
        ),
    )
    store.save_entries(root, [entry])
    return entry


def test_busy_publication_repair_does_not_wait_or_change_lease(tmp_path: Path) -> None:
    entry = _pending_publication(tmp_path)
    before = (tmp_path / store.QUEUE_FILE_NAME).read_bytes()
    published: list[object] = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        with queue_record_publication_lock(tmp_path, entry.queue_id):
            future = pool.submit(
                repair_enqueue_publication_outcome,
                tmp_path,
                entry,
                label="test",
                publish=published.append,
                lock_timeout_seconds=0,
            )
            assert future.result(timeout=1).reason == "busy"
            assert not published
            assert (tmp_path / store.QUEUE_FILE_NAME).read_bytes() == before
    assert repair_enqueue_publication_outcome(
        tmp_path, entry, label="test", publish=published.append, lock_timeout_seconds=0
    ).repaired
    assert len(published) == 1


def test_publication_callback_timeout_is_failure_not_busy(tmp_path: Path) -> None:
    entry = _pending_publication(tmp_path)

    def publish(_entry: QueueEntry) -> None:
        raise FileLockTimeoutError("publisher internal timeout")

    outcome = repair_enqueue_publication_outcome(
        tmp_path, entry, label="test", publish=publish, lock_timeout_seconds=0
    )
    assert outcome.reason == "failed"
    assert isinstance(outcome.error, FileLockTimeoutError)
    assert queue_record_sync_state(store.load_entries(tmp_path)[0]) == "repair_pending"
