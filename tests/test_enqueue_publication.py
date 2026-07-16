from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.queue import enqueue_publication as driver
from orca_auto.core.queue.enqueue_publication import (
    EnqueuePublicationOutcomeUnknown,
    EnqueuePublicationSpec,
    _recover_committed_enqueue,
    repair_enqueue_publication,
    run_enqueue_publication,
)
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    queue_record_sync_metadata,
)
from orca_auto.core.queue.store import enqueue, list_queue
from orca_auto.core.queue.types import QueueStatus


def _allow_duplicates(*_args: Any, **_kwargs: Any) -> None:
    return None


def _spec(queue_root: Path, **overrides: Any) -> EnqueuePublicationSpec:
    fields: dict[str, Any] = {
        "queue_root": queue_root,
        "app_name": "test_app",
        "task_id": "task-1",
        "task_kind": "kind",
        "engine": "test_engine",
        "priority": 10,
        "metadata": {"job_dir": str(queue_root / "job")},
        "label": "TEST",
        "publish": lambda _entry: None,
        "duplicate_policy": _allow_duplicates,
    }
    fields.update(overrides)
    return EnqueuePublicationSpec(**fields)


def _enqueue_preparing_row(
    queue_root: Path,
    *,
    task_id: str,
    token: str,
    job_dir: Path,
) -> Any:
    metadata: dict[str, Any] = {"job_dir": str(job_dir)}
    metadata.update(
        queue_record_sync_metadata(
            QUEUE_RECORD_SYNC_PREPARING,
            token=token,
            owner_pid=os.getpid(),
        )
    )
    return enqueue(
        queue_root,
        app_name="test_app",
        task_id=task_id,
        task_kind="kind",
        engine="test_engine",
        priority=10,
        metadata=metadata,
        duplicate_policy=_allow_duplicates,
    )


def test_recovery_fences_every_ambiguous_identity_match(tmp_path: Path) -> None:
    # Two rows carrying the full strict identity of one enqueue attempt can
    # never be disambiguated; leaving either alive would eventually let a
    # stale PREPARING row be repaired and run for an attempt that failed.
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    token = "ambiguous-token"
    first = _enqueue_preparing_row(tmp_path, task_id="task-1", token=token, job_dir=job_dir)
    second = _enqueue_preparing_row(tmp_path, task_id="task-1", token=token, job_dir=job_dir)
    assert first.queue_id != second.queue_id
    spec = _spec(tmp_path)
    enqueue_metadata = dict(first.metadata)

    recovered = _recover_committed_enqueue(spec, enqueue_metadata=enqueue_metadata)

    assert recovered is None
    rows = list_queue(tmp_path)
    assert len(rows) == 2
    for row in rows:
        assert row.status == QueueStatus.CANCELLED
        assert row.cancel_requested is True
        assert row.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED
        assert row.metadata[QUEUE_RECORD_SYNC_TOKEN_KEY] == ""


def test_recovery_scan_failure_reports_outcome_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_enqueue = driver.enqueue

    def commit_then_lose(*args: Any, **kwargs: Any) -> Any:
        real_enqueue(*args, **kwargs)
        raise OSError("durability barrier failed after the enqueue committed")

    def broken_scan(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("queue store unreadable during recovery")

    monkeypatch.setattr(driver, "enqueue", commit_then_lose)
    monkeypatch.setattr(driver, "mutate_entries", broken_scan)

    with pytest.raises(EnqueuePublicationOutcomeUnknown) as excinfo:
        run_enqueue_publication(_spec(tmp_path))

    assert "indeterminate after recovery failure" in str(excinfo.value)
    assert "durability barrier failed" in str(excinfo.value)


def test_repair_publish_failure_parks_with_fresh_token(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    entry = _enqueue_preparing_row(
        tmp_path,
        task_id="task-1",
        token="submitter-token",
        job_dir=job_dir,
    )

    def failing_publish(_entry: Any) -> None:
        raise OSError("queued artifact write failed")

    assert (
        repair_enqueue_publication(
            tmp_path,
            entry,
            publish=failing_publish,
            label="TEST",
        )
        is False
    )
    [row] = list_queue(tmp_path)
    assert row.status == QueueStatus.PENDING
    assert row.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING
    # The repair lease minted its own token; the submitter's token must not
    # survive the failed repair, or the original publisher could CAS over it.
    assert row.metadata[QUEUE_RECORD_SYNC_TOKEN_KEY] != "submitter-token"


def test_repair_base_exception_parks_then_propagates(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    entry = _enqueue_preparing_row(
        tmp_path,
        task_id="task-1",
        token="submitter-token",
        job_dir=job_dir,
    )

    def interrupted_publish(_entry: Any) -> None:
        raise KeyboardInterrupt("operator interrupt during repair publish")

    with pytest.raises(KeyboardInterrupt):
        repair_enqueue_publication(
            tmp_path,
            entry,
            publish=interrupted_publish,
            label="TEST",
        )
    [row] = list_queue(tmp_path)
    assert row.status == QueueStatus.PENDING
    assert row.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING


def test_publication_keyboard_interrupt_parks_then_propagates(tmp_path: Path) -> None:
    (tmp_path / "job").mkdir()

    def interrupted_publish(_entry: Any) -> None:
        raise KeyboardInterrupt("operator interrupt during publication")

    with pytest.raises(KeyboardInterrupt):
        run_enqueue_publication(_spec(tmp_path, publish=interrupted_publish))
    [row] = list_queue(tmp_path)
    assert row.status == QueueStatus.PENDING
    assert row.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING
