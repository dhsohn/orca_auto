from __future__ import annotations

import os
import signal
from datetime import UTC, datetime
from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.queue import publication, store
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_OWNER_START_KEY,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QUEUE_RECORD_SYNC_REPAIRING,
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
from orca_auto.orca.app_ids import ORCA_AUTO_ORCA_APP_NAME, ORCA_ENGINE, ORCA_TASK_KIND
from orca_auto.orca.queue import adapter as queue_adapter
from orca_auto.orca.queue import enqueue_publication as driver
from orca_auto.orca.queue import entries as queue_entries
from orca_auto.orca.queue.enqueue_publication import (
    EnqueuePublicationOutcomeUnknown,
    EnqueuePublicationSpec,
    _recover_committed_enqueue,
    repair_enqueue_publication_outcome,
    run_enqueue_publication,
)
from tests.conftest import enqueue_entry, make_queue_entry
from tests.queue_store_helpers import _claim_next, _enqueue


def _enqueue_via_adapter(root: Path, **kwargs: Any) -> QueueEntry:
    # The same adapter call ``orca.submission`` wires as ``enqueue_fn``.
    return queue_adapter.enqueue(
        root,
        kwargs["metadata"]["reaction_dir"],
        priority=kwargs["priority"],
        task_id=kwargs["task_id"],
        task_kind=kwargs["task_kind"],
        metadata=kwargs["metadata"],
        before_commit_fn=kwargs.get("before_commit_fn"),
        after_commit_fn=kwargs.get("after_commit_fn"),
    )


def _mark_failed_via_adapter(root: Path, queue_id: str, **kwargs: Any) -> Any:
    return queue_adapter.mark_failed(
        root,
        queue_id,
        publish_terminal_side_effects=False,
        **kwargs,
    )


def _spec(queue_root: Path, **overrides: Any) -> EnqueuePublicationSpec:
    fields: dict[str, Any] = {
        "queue_root": queue_root,
        "app_name": ORCA_AUTO_ORCA_APP_NAME,
        "task_id": "task-1",
        "task_kind": ORCA_TASK_KIND,
        "engine": ORCA_ENGINE,
        "priority": 10,
        "metadata": {"reaction_dir": str(queue_root / "job")},
        "label": "TEST",
        "publish": lambda _entry: None,
        "enqueue_fn": _enqueue_via_adapter,
        "mark_failed_fn": _mark_failed_via_adapter,
        "same_generation": queue_entries.same_generation,
        "job_dir_metadata_key": "reaction_dir",
    }
    fields.update(overrides)
    return EnqueuePublicationSpec(**fields)


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


def test_park_queue_record_repair_pending_preserves_cancel_flag() -> None:
    entry = QueueEntry(
        queue_id="queue-1",
        app_name="test_app",
        task_id="task-1",
        task_kind="kind",
        engine="test_engine",
        cancel_requested=True,
        metadata=queue_record_sync_metadata(
            QUEUE_RECORD_SYNC_PREPARING,
            token="owned-token",
            owner_pid=os.getpid(),
        ),
    )
    entries = [entry]

    result, changed = driver._park_queue_record_repair_pending(
        entries,
        entry,
        expected_state=QUEUE_RECORD_SYNC_PREPARING,
        expected_token="owned-token",
    )

    assert result is None
    assert changed is True
    assert entries[0].cancel_requested is True
    assert entries[0].metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING
    assert entries[0].metadata[QUEUE_RECORD_SYNC_TOKEN_KEY] == "owned-token"
    assert entries[0].metadata[QUEUE_RECORD_SYNC_OWNER_PID_KEY] == 0


@pytest.mark.parametrize(
    ("expected_state", "expected_token"),
    [
        (QUEUE_RECORD_SYNC_REPAIRING, "owned-token"),
        (QUEUE_RECORD_SYNC_PREPARING, "foreign-token"),
    ],
)
def test_park_queue_record_repair_pending_refuses_ownership_mismatch(
    expected_state: str,
    expected_token: str,
) -> None:
    entry = QueueEntry(
        queue_id="queue-1",
        app_name="test_app",
        task_id="task-1",
        task_kind="kind",
        engine="test_engine",
        metadata=queue_record_sync_metadata(
            QUEUE_RECORD_SYNC_PREPARING,
            token="owned-token",
            owner_pid=os.getpid(),
        ),
    )
    entries = [entry]

    result, changed = driver._park_queue_record_repair_pending(
        entries,
        entry,
        expected_state=expected_state,
        expected_token=expected_token,
    )

    assert result is None
    assert changed is False
    assert entries == [entry]


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

    with pytest.raises(EnqueuePublicationOutcomeUnknown):
        _recover_committed_enqueue(spec, enqueue_metadata=enqueue_metadata)

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
    def commit_then_lose(*args: Any, **kwargs: Any) -> Any:
        _enqueue_via_adapter(*args, **kwargs)
        raise OSError("durability barrier failed after the enqueue committed")

    def broken_scan(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("queue store unreadable during recovery")

    monkeypatch.setattr(driver, "mutate_entries", broken_scan)

    with pytest.raises(EnqueuePublicationOutcomeUnknown) as excinfo:
        run_enqueue_publication(_spec(tmp_path, enqueue_fn=commit_then_lose))

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
        repair_enqueue_publication_outcome(
            tmp_path,
            entry,
            publish=failing_publish,
            label="TEST",
            same_generation=queue_entries.same_generation,
        ).repaired
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
        repair_enqueue_publication_outcome(
            tmp_path,
            entry,
            publish=interrupted_publish,
            label="TEST",
            same_generation=queue_entries.same_generation,
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
    monkeypatch.setattr(driver, "mutate_entries", broken_scan)

    with pytest.raises(QueueLockTimeoutError, match="held by another process"):
        run_enqueue_publication(
            _spec(
                tmp_path,
                enqueue_fn=busy_enqueue,
                on_compensated_failure=lambda: compensations.append("cleaned"),
            )
        )

    assert compensations == ["cleaned"]
    assert not (tmp_path / "queue.json").exists()


def test_pre_commit_corrupt_store_is_reported_as_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def corrupt_enqueue(*_args: Any, **_kwargs: Any) -> Any:
        raise QueueStoreCorruptError("queue.json is not a list")

    monkeypatch.setattr(driver, "mutate_entries", lambda *_a, **_k: pytest.fail("no recovery scan"))

    with pytest.raises(QueueStoreCorruptError):
        run_enqueue_publication(_spec(tmp_path, enqueue_fn=corrupt_enqueue))


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
    assert _claim_next(tmp_path) is None
    [parked] = store.list_queue(tmp_path)
    assert parked.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_PREPARING

    published: list[str] = []
    assert repair_enqueue_publication_outcome(
        tmp_path,
        parked,
        publish=lambda current: published.append(current.queue_id),
        label="test",
        same_generation=queue_entries.same_generation,
    ).repaired
    assert published == [queue_id]

    [repaired] = store.list_queue(tmp_path)
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE

    claimed = _claim_next(tmp_path)

    assert claimed is not None
    assert claimed.queue_id == queue_id
