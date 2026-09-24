from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from orca_auto import cli_queue
from orca_auto.activity._orca import queue_record
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    queue_record_sync_metadata,
)
from orca_auto.core.queue.types import QueueStatus
from orca_auto.orca.queue import adapter, publication_repair
from tests.queue_worker_helpers import current_orca_queue_metadata, make_queue_worker_cfg


def _pending_entry(root: Path):
    reaction = root / "job"
    return adapter.enqueue(
        root,
        str(reaction),
        task_id="repair-task",
        metadata=current_orca_queue_metadata(
            reaction,
            queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_REPAIR_PENDING, token="test-lease", owner_pid=0
            ),
        ),
    )


def test_publication_failure_persists_reason_and_clears_after_repair(tmp_path: Path) -> None:
    cfg = make_queue_worker_cfg(str(tmp_path))
    entry = _pending_entry(tmp_path)
    generation = queue_entry_generation_token(entry)
    with patch.object(
        publication_repair, "upsert_queued_job_record", side_effect=OSError("index unavailable")
    ):
        assert not publication_repair.repair_queue_publication(cfg, tmp_path, entry)
    [blocked] = adapter.list_queue(tmp_path)
    assert blocked.status == QueueStatus.PENDING
    assert queue_entry_generation_token(blocked) == generation
    metadata = queue_record(adapter, blocked, None, allowed_root=tmp_path).metadata
    assert "index unavailable" in metadata["publication_blocked_reason"]
    assert metadata["publication_blocked_scope"] == "orca_queue"
    assert entry.queue_id in metadata["publication_blocked_action"]
    assert adapter.dequeue_next(tmp_path) is None

    with patch.object(publication_repair, "upsert_queued_job_record"):
        assert publication_repair.repair_queue_publication(cfg, tmp_path, blocked)
    [repaired] = adapter.list_queue(tmp_path)
    metadata = queue_record(adapter, repaired, None, allowed_root=tmp_path).metadata
    assert metadata["publication_blocked_reason"] == ""
    assert queue_entry_generation_token(repaired) == generation
    assert adapter.dequeue_next(tmp_path) is not None


def test_filtered_list_still_explains_queue_wide_publication_block(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The listing reports blockers for the whole catalog even when the status
    # filter leaves the page empty; the CLI prints them under the empty table.
    blocker = {
        "queue_id": "blocked-row",
        "allowed_root": "/runs",
        "scope": "orca_queue",
        "reason": "index unavailable",
        "next_action": "Restore index access; worker retries, or queue cancel blocked-row.",
    }
    monkeypatch.setattr(cli_queue, "_layout_interactive", lambda: False)
    cli_queue._print_queue_list_text(
        payload={
            "count": 0,
            "active_simulations": 0,
            "activities": [],
            "sources": {},
            "admission_blockers": [blocker],
        }
    )
    output = capsys.readouterr().out
    assert "index unavailable" in output
    assert "queue cancel blocked-row" in output


def test_old_generation_cannot_publish_a_blocker_on_replacement(tmp_path: Path) -> None:
    cfg = make_queue_worker_cfg(str(tmp_path))
    entry = _pending_entry(tmp_path)
    from orca_auto.core.queue.store import mutate_entries

    def replace_row(entries):
        entries[0] = replace(entries[0], task_id="replacement")
        return None, True

    mutate_entries(tmp_path, replace_row)
    assert not publication_repair.repair_queue_publication(cfg, tmp_path, entry)
    [current] = adapter.list_queue(tmp_path)
    assert current.task_id == "replacement"
    assert not queue_record(adapter, current, None, allowed_root=tmp_path).metadata.get(
        "publication_blocked_reason"
    )


def test_complete_publication_clears_resolved_path_error(tmp_path: Path) -> None:
    cfg = make_queue_worker_cfg(str(tmp_path))
    entry = _pending_entry(tmp_path)
    assert adapter.update_metadata(
        tmp_path,
        entry.queue_id,
        {
            "selected_inp": str(tmp_path / "job" / "input.inp"),
            **queue_record_sync_metadata(QUEUE_RECORD_SYNC_COMPLETE, token="complete", owner_pid=0),
        },
    )
    [entry] = adapter.list_queue(tmp_path)
    selected = Path(entry.metadata["selected_inp"])
    original_resolve = Path.resolve

    def interrupted_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == selected:
            raise PermissionError("temporary input path error")
        return original_resolve(path)

    with patch.object(Path, "resolve", interrupted_resolve):
        assert not publication_repair.repair_queue_publication(cfg, tmp_path, entry)
    [blocked] = adapter.list_queue(tmp_path)
    assert queue_record(adapter, blocked, None, allowed_root=tmp_path).metadata[
        "publication_blocked_reason"
    ]
    assert publication_repair.repair_queue_publication(cfg, tmp_path, blocked)
    [recovered] = adapter.list_queue(tmp_path)
    assert not queue_record(adapter, recovered, None, allowed_root=tmp_path).metadata[
        "publication_blocked_reason"
    ]
