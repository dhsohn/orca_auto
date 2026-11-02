from __future__ import annotations

import os
from pathlib import Path

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
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from tests.queue_store_helpers import _claim_next, _enqueue, _install_deterministic_helpers


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
        metadata[QUEUE_RECORD_SYNC_OWNER_START_KEY] = process_start_token(os.getpid())
    blocked = _enqueue(
        tmp_path,
        app_name="app",
        task_id="blocked",
        task_kind="kind",
        engine="engine",
        priority=1,
        metadata=metadata,
    )
    ready = _enqueue(
        tmp_path,
        app_name="app",
        task_id="ready",
        task_kind="kind",
        engine="engine",
        priority=9,
    )

    assert store.dequeue_entry_if_pending(tmp_path, blocked.queue_id) is None
    claimed = _claim_next(tmp_path)

    # A parked publication must not stall the rows behind it.
    assert claimed is not None
    assert claimed.queue_id == ready.queue_id


@pytest.mark.parametrize("sync_state", ["repair_pendng", QUEUE_RECORD_SYNC_ABORTED])
def test_dequeue_quarantines_unknown_or_aborted_publication_state(
    tmp_path: Path,
    sync_state: str,
) -> None:
    blocked = _enqueue(
        tmp_path,
        app_name="app",
        task_id="blocked",
        task_kind="kind",
        engine="engine",
        metadata={QUEUE_RECORD_SYNC_KEY: sync_state},
    )

    assert store.dequeue_entry_if_pending(tmp_path, blocked.queue_id) is None
    assert _claim_next(tmp_path) is None


def test_publication_lock_does_not_create_a_missing_queue_root(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing"

    with pytest.raises(FileNotFoundError):
        with publication.queue_record_publication_lock(missing_root, "q-1"):
            pass

    assert not missing_root.exists()


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

    result, changed = publication.park_queue_record_repair_pending(
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

    result, changed = publication.park_queue_record_repair_pending(
        entries,
        entry,
        expected_state=expected_state,
        expected_token=expected_token,
    )

    assert result is None
    assert changed is False
    assert entries == [entry]


def _leased_entry(
    sync_state: str,
    *,
    token: str = "owned-token",
    queue_id: str = "queue-1",
    status: QueueStatus = QueueStatus.PENDING,
    cancel_requested: bool = False,
) -> QueueEntry:
    return QueueEntry(
        queue_id=queue_id,
        app_name="test_app",
        task_id="task-1",
        task_kind="kind",
        engine="test_engine",
        status=status,
        cancel_requested=cancel_requested,
        metadata=queue_record_sync_metadata(sync_state, token=token, owner_pid=os.getpid()),
    )


@pytest.mark.parametrize("lease_state", [QUEUE_RECORD_SYNC_PREPARING, QUEUE_RECORD_SYNC_REPAIRING])
def test_complete_queue_record_lease_completes_the_owned_lease(lease_state: str) -> None:
    entry = _leased_entry(lease_state)
    other = _leased_entry(lease_state, token="other-token", queue_id="queue-2")
    entries = [other, entry]

    result, changed = publication.complete_queue_record_lease(
        entries, entry, expected_state=lease_state, expected_token="owned-token"
    )

    assert changed is True
    assert result is entries[1]
    assert entries[0] is other
    assert entries[1].metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
    assert entries[1].metadata[QUEUE_RECORD_SYNC_TOKEN_KEY] == "owned-token"
    assert entries[1].metadata[QUEUE_RECORD_SYNC_OWNER_PID_KEY] == 0
    assert entries[1].metadata[QUEUE_RECORD_SYNC_OWNER_START_KEY] == ""


@pytest.mark.parametrize(
    ("row", "expected_state", "expected_token"),
    [
        (_leased_entry(QUEUE_RECORD_SYNC_PREPARING), QUEUE_RECORD_SYNC_REPAIRING, "owned-token"),
        (_leased_entry(QUEUE_RECORD_SYNC_REPAIRING), QUEUE_RECORD_SYNC_PREPARING, "owned-token"),
        (_leased_entry(QUEUE_RECORD_SYNC_PREPARING), QUEUE_RECORD_SYNC_PREPARING, "foreign"),
        (
            _leased_entry(QUEUE_RECORD_SYNC_PREPARING, cancel_requested=True),
            QUEUE_RECORD_SYNC_PREPARING,
            "owned-token",
        ),
        (
            _leased_entry(QUEUE_RECORD_SYNC_PREPARING, status=QueueStatus.CANCELLED),
            QUEUE_RECORD_SYNC_PREPARING,
            "owned-token",
        ),
        (_leased_entry(QUEUE_RECORD_SYNC_COMPLETE), QUEUE_RECORD_SYNC_PREPARING, "owned-token"),
    ],
)
def test_complete_queue_record_lease_never_overwrites_a_lost_lease(
    row: QueueEntry, expected_state: str, expected_token: str
) -> None:
    entries = [row]

    result, changed = publication.complete_queue_record_lease(
        entries, row, expected_state=expected_state, expected_token=expected_token
    )

    assert changed is False
    assert result is row
    assert entries == [row]


def test_complete_queue_record_lease_reports_a_missing_row() -> None:
    entry = _leased_entry(QUEUE_RECORD_SYNC_PREPARING)

    result, changed = publication.complete_queue_record_lease(
        [], entry, expected_state=QUEUE_RECORD_SYNC_PREPARING, expected_token="owned-token"
    )

    assert (result, changed) == (None, False)
