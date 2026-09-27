from __future__ import annotations

import os
from pathlib import Path

import pytest

from orca_auto.core.queue import publication, store
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_OWNER_START_KEY,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_REPAIRING,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
    process_start_token,
)
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
