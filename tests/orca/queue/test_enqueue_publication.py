from __future__ import annotations

import os
import signal
from collections.abc import Callable
from datetime import UTC, datetime
from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.artifacts import QUEUE_FILE
from orca_auto.core.queue import persistence, publication, store
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_OWNER_START_KEY,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
    process_start_token,
    queue_record_sync_metadata,
)
from orca_auto.core.queue.store import (
    QueueLockTimeoutError,
    QueueStoreCorruptError,
    list_queue,
)
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.queue import adapter as queue_adapter
from orca_auto.orca.queue import enqueue_publication as driver
from orca_auto.orca.queue import publication_repair
from orca_auto.orca.queue.enqueue_publication import (
    EnqueuePublicationOutcome,
    EnqueuePublicationOutcomeUnknown,
    _recover_committed_enqueue,
    run_enqueue_publication,
)
from orca_auto.orca.queue.entries import TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY
from orca_auto.orca.queue.publication_repair import repair_enqueue_publication_outcome
from tests.conftest import enqueue_entry, make_app_cfg, make_queue_entry
from tests.queue_store_helpers import _claim_next, _enqueue


def _submit(
    queue_root: Path,
    *,
    on_compensated_failure: Callable[[], None] = lambda: None,
) -> EnqueuePublicationOutcome:
    job_dir = queue_root / "job"
    return run_enqueue_publication(
        make_app_cfg(queue_root),
        job_dir,
        task_id="task-1",
        priority=10,
        force=False,
        metadata={"reaction_dir": str(job_dir)},
        on_compensated_failure=on_compensated_failure,
    )


def _enqueue_preparing_row(
    queue_root: Path,
    *,
    task_id: str,
    token: str,
    job_dir: Path,
) -> QueueEntry:
    # Written directly so a test can persist rows the adapter would reject as
    # duplicates of one reaction directory.
    return enqueue_entry(
        queue_root,
        make_queue_entry(
            task_id=task_id,
            reaction_dir=job_dir,
            metadata=queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_PREPARING,
                token=token,
                owner_pid=os.getpid(),
            ),
        ),
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
    enqueue_metadata = dict(first.metadata)

    with pytest.raises(EnqueuePublicationOutcomeUnknown):
        _recover_committed_enqueue(
            tmp_path, task_id="task-1", priority=10, enqueue_metadata=enqueue_metadata
        )

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
    real_enqueue = queue_adapter.enqueue

    def commit_then_lose(*args: Any, **kwargs: Any) -> Any:
        real_enqueue(*args, **kwargs)
        raise OSError("durability barrier failed after the enqueue committed")

    def broken_scan(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("queue store unreadable during recovery")

    monkeypatch.setattr(queue_adapter, "enqueue", commit_then_lose)
    monkeypatch.setattr(driver, "mutate_entries", broken_scan)

    with pytest.raises(EnqueuePublicationOutcomeUnknown) as excinfo:
        _submit(tmp_path)

    assert "indeterminate after recovery failure" in str(excinfo.value)
    assert "durability barrier failed" in str(excinfo.value)


def test_repair_publish_failure_parks_with_fresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    entry = _enqueue_preparing_row(
        tmp_path,
        task_id="task-1",
        token="submitter-token",
        job_dir=job_dir,
    )

    def failing_publish(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("queued artifact write failed")

    monkeypatch.setattr(publication_repair, "upsert_row_job_record", failing_publish)

    assert (
        repair_enqueue_publication_outcome(make_app_cfg(tmp_path), tmp_path, entry).repaired
        is False
    )
    [row] = list_queue(tmp_path)
    assert row.status == QueueStatus.PENDING
    assert row.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING
    # The repair lease minted its own token; the submitter's token must not
    # survive the failed repair, or the original publisher could CAS over it.
    assert row.metadata[QUEUE_RECORD_SYNC_TOKEN_KEY] != "submitter-token"


def test_repair_base_exception_parks_then_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    entry = _enqueue_preparing_row(
        tmp_path,
        task_id="task-1",
        token="submitter-token",
        job_dir=job_dir,
    )

    def interrupted_publish(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt("operator interrupt during repair publish")

    monkeypatch.setattr(publication_repair, "upsert_row_job_record", interrupted_publish)

    with pytest.raises(KeyboardInterrupt):
        repair_enqueue_publication_outcome(make_app_cfg(tmp_path), tmp_path, entry)
    [row] = list_queue(tmp_path)
    assert row.status == QueueStatus.PENDING
    assert row.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING


def test_publication_keyboard_interrupt_parks_then_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "job").mkdir()

    def interrupted_publish(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt("operator interrupt during publication")

    monkeypatch.setattr(driver, "upsert_row_job_record", interrupted_publish)

    with pytest.raises(KeyboardInterrupt):
        _submit(tmp_path)
    [row] = list_queue(tmp_path)
    assert row.status == QueueStatus.PENDING
    assert row.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING


def test_pre_commit_lock_timeout_is_reported_as_itself_and_compensated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The queue lock is taken before any write: a timeout there means nothing
    # was enqueued. It used to be re-scanned under the same lock, fail again,
    # and surface as "outcome unknown" with the submission snapshot retained.
    def busy_enqueue(*_args: Any, **_kwargs: Any) -> Any:
        raise QueueLockTimeoutError("queue.lock is held by another process")

    def broken_scan(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("no recovery scan for a failure that committed nothing")

    compensations: list[str] = []
    monkeypatch.setattr(queue_adapter, "enqueue", busy_enqueue)
    monkeypatch.setattr(driver, "mutate_entries", broken_scan)

    with pytest.raises(QueueLockTimeoutError, match="held by another process"):
        _submit(tmp_path, on_compensated_failure=lambda: compensations.append("cleaned"))

    assert compensations == ["cleaned"]
    assert not (tmp_path / "queue.json").exists()


def test_pre_commit_corrupt_store_is_reported_as_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def corrupt_enqueue(*_args: Any, **_kwargs: Any) -> Any:
        raise QueueStoreCorruptError("queue.json is not a list")

    monkeypatch.setattr(queue_adapter, "enqueue", corrupt_enqueue)
    monkeypatch.setattr(driver, "mutate_entries", lambda *_a, **_k: pytest.fail("no recovery scan"))

    with pytest.raises(QueueStoreCorruptError):
        _submit(tmp_path)


def _enqueue_transient_publisher_then_crash(
    queue_root: str,
    ready_connection: Connection,
) -> None:
    entry = _enqueue(
        queue_root,
        app_name="app",
        task_id="crashed-publisher",
        task_kind="kind",
        engine="engine",
        metadata={
            QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_PREPARING,
            QUEUE_RECORD_SYNC_OWNER_PID_KEY: os.getpid(),
            QUEUE_RECORD_SYNC_OWNER_START_KEY: process_start_token(os.getpid()),
            QUEUE_RECORD_SYNC_UPDATED_AT_KEY: datetime.now(UTC).isoformat(),
        },
    )
    # Die while owning the same process-scoped lock used around publication.
    # The kernel must release it so cancellation/repair can recover the entry.
    with publication.queue_record_publication_lock(queue_root, entry.queue_id):
        ready_connection.send(entry.queue_id)
        os.kill(os.getpid(), signal.SIGKILL)


def test_sigkilled_publisher_row_stays_parked_until_repair_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    assert _claim_next(tmp_path) is None
    [parked] = store.list_queue(tmp_path)
    assert parked.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_PREPARING

    published: list[str] = []
    monkeypatch.setattr(
        publication_repair,
        "upsert_row_job_record",
        lambda _cfg, current, *_args, **_kwargs: published.append(current.queue_id),
    )
    assert repair_enqueue_publication_outcome(make_app_cfg(tmp_path), tmp_path, parked).repaired
    assert published == [queue_id]

    [repaired] = store.list_queue(tmp_path)
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE

    claimed = _claim_next(tmp_path)

    assert claimed is not None
    assert claimed.queue_id == queue_id


def test_enqueue_recovery_never_matches_a_foreign_row(tmp_path: Path) -> None:
    root = tmp_path / "shared_queue"
    reaction_dir = str((root / "reaction").resolve())
    token = "publication-token"
    metadata = {
        "reaction_dir": reaction_dir,
        "force": False,
        "_orca_auto_queued_record_sync": "preparing",
        "_orca_auto_queued_record_sync_token": token,
        "_orca_auto_queued_record_sync_owner_pid": 1234,
        "_orca_auto_queued_record_sync_owner_start": "owner-start",
    }
    foreign = QueueEntry(
        queue_id="queue-foreign",
        app_name="orca_auto_orca",
        task_id="task-ambiguous",
        task_kind="other_sp",
        engine="orca",
        priority=7,
        metadata=dict(metadata),
    )
    persistence.save_entries(root, [foreign])
    queue_path = root / QUEUE_FILE
    before = queue_path.read_bytes()

    recovered = _recover_committed_enqueue(
        root, task_id="task-ambiguous", priority=7, enqueue_metadata=dict(metadata)
    )

    # The row differs in task_kind: the strict identity match refuses it even
    # though it carries this attempt's publication token, and nothing mutates.
    assert recovered is None
    assert queue_path.read_bytes() == before


def test_ambiguous_enqueue_recovery_is_durable_fence_only_history(tmp_path: Path) -> None:
    root = tmp_path / "shared_queue"
    reaction_dir = root / "reaction"
    reaction_dir.mkdir(parents=True)
    token = "ambiguous-publication-token"
    metadata = {
        "reaction_dir": str(reaction_dir.resolve()),
        "force": False,
        "_orca_auto_queued_record_sync": "preparing",
        "_orca_auto_queued_record_sync_token": token,
        "_orca_auto_queued_record_sync_owner_pid": 1234,
        "_orca_auto_queued_record_sync_owner_start": "owner-start",
    }
    entries = [
        QueueEntry(
            queue_id=queue_id,
            app_name="orca_auto_orca",
            task_id="task-ambiguous",
            task_kind="orca_run_inp",
            engine="orca",
            priority=7,
            metadata=dict(metadata),
        )
        for queue_id in ("queue-a", "queue-b")
    ]
    persistence.save_entries(root, entries)

    with pytest.raises(EnqueuePublicationOutcomeUnknown):
        _recover_committed_enqueue(
            root, task_id="task-ambiguous", priority=7, enqueue_metadata=dict(metadata)
        )

    fenced = queue_adapter.list_queue(root)
    assert len(fenced) == 2
    assert {entry.status for entry in fenced} == {QueueStatus.CANCELLED}
    # The administrative fence-only marker keeps a successor generation for
    # the same reaction_dir blocked until the ambiguous rows are cleared.
    assert all(
        entry.metadata.get(TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY) is True for entry in fenced
    )
