from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from orca_auto.core.artifacts import QUEUE_FILE
from orca_auto.core.queue import persistence
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_PREPARING,
    queue_record_publication_lock,
    queue_record_sync_metadata,
    queue_record_sync_state,
)
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.utils.lock import FileLockTimeoutError
from orca_auto.orca.queue import publication_repair
from orca_auto.orca.queue.publication_repair import repair_enqueue_publication_outcome
from tests.conftest import make_app_cfg


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
    persistence.save_entries(root, [entry])
    return entry


def test_busy_publication_repair_does_not_wait_or_change_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _pending_publication(tmp_path)
    before = (tmp_path / QUEUE_FILE).read_bytes()
    published: list[object] = []
    monkeypatch.setattr(
        publication_repair,
        "upsert_row_job_record",
        lambda _cfg, current, *_args, **_kwargs: published.append(current),
    )
    cfg = make_app_cfg(tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with queue_record_publication_lock(tmp_path, entry.queue_id):
            future = pool.submit(repair_enqueue_publication_outcome, cfg, tmp_path, entry)
            assert future.result(timeout=1).reason == "busy"
            assert not published
            assert (tmp_path / QUEUE_FILE).read_bytes() == before
    assert repair_enqueue_publication_outcome(cfg, tmp_path, entry).repaired
    assert len(published) == 1


def test_publication_callback_timeout_is_failure_not_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _pending_publication(tmp_path)

    def publish(*_args: object, **_kwargs: object) -> None:
        raise FileLockTimeoutError("publisher internal timeout")

    monkeypatch.setattr(publication_repair, "upsert_row_job_record", publish)

    outcome = repair_enqueue_publication_outcome(make_app_cfg(tmp_path), tmp_path, entry)
    assert outcome.reason == "failed"
    assert isinstance(outcome.error, FileLockTimeoutError)
    assert queue_record_sync_state(persistence.load_entries(tmp_path)[0]) == "repair_pending"
