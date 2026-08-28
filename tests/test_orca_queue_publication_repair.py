"""Tests for ORCA queue publication repair."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    queue_record_sync_metadata,
)
from orca_auto.core.queue.store import enqueue as enqueue_core
from orca_auto.core.queue.store import list_queue as list_queue_core
from orca_auto.core.queue.store import update_metadata
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.queue import publication_repair as publication_mod
from orca_auto.orca.queue.adapter import dequeue_next, enqueue, list_queue
from orca_auto.orca.queue.terminal_replay import (
    TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY,
)
from tests.queue_worker_helpers import (
    current_orca_queue_metadata as _current_orca_queue_metadata,
)
from tests.queue_worker_helpers import make_queue_worker_cfg as _make_cfg


def test_orca_worker_repairs_queued_publication_before_claim(tmp_path: Path) -> None:
    cfg = _make_cfg(str(tmp_path))
    reaction_dir = tmp_path / "rxn"
    metadata = _current_orca_queue_metadata(
        reaction_dir,
        {
            "selected_input_xyz": str(reaction_dir / "input.xyz"),
            "job_type": "opt",
            "molecule_key": "mol",
            "resource_request": {"max_cores": 1, "max_memory_gb": 1},
            **queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_REPAIR_PENDING,
                token="repair-token",
                owner_pid=0,
            ),
        },
    )
    entry = enqueue(
        tmp_path,
        str(reaction_dir),
        task_id="task-repair",
        metadata=metadata,
    )
    upserts: list[str] = []

    with patch.object(
        publication_mod,
        "upsert_queued_job_record",
        side_effect=lambda _cfg, current: upserts.append(current.task_id),
    ):
        assert publication_mod.repair_queue_publication(cfg, tmp_path, entry) is True

    [repaired] = list_queue(tmp_path)
    assert upserts == ["task-repair"]
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE


def test_orca_worker_keeps_failed_publication_repair_unclaimable(tmp_path: Path) -> None:
    cfg = _make_cfg(str(tmp_path))
    reaction_dir = tmp_path / "rxn"
    entry = enqueue(
        tmp_path,
        str(reaction_dir),
        task_id="task-repair",
        metadata=_current_orca_queue_metadata(
            reaction_dir,
            queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_REPAIR_PENDING,
                token="repair-token",
                owner_pid=0,
            ),
        ),
    )

    with patch.object(
        publication_mod,
        "upsert_queued_job_record",
        side_effect=OSError("index unavailable"),
    ):
        assert publication_mod.repair_queue_publication(cfg, tmp_path, entry) is False

    [pending] = list_queue(tmp_path)
    assert pending.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING
    assert dequeue_next(tmp_path) is None


def test_orca_publication_repair_ignores_foreign_engine_row(tmp_path: Path) -> None:
    cfg = _make_cfg(str(tmp_path))
    foreign = enqueue_core(
        tmp_path,
        app_name="orca_auto_xtb",
        task_id="xtb-foreign",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(tmp_path / "xtb-job"),
            **queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_REPAIR_PENDING,
                token="foreign-token",
                owner_pid=0,
            ),
        },
    )

    with patch.object(publication_mod, "upsert_queued_job_record") as upsert:
        assert publication_mod.repair_queue_publication(cfg, tmp_path, foreign)

    upsert.assert_not_called()
    [unchanged] = list_queue_core(tmp_path)
    assert unchanged.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING


def test_orca_publication_repair_reclaims_abandoned_live_pid_lease(tmp_path: Path) -> None:
    cfg = _make_cfg(str(tmp_path))
    reaction_dir = tmp_path / "rxn"
    entry = enqueue(
        tmp_path,
        str(reaction_dir),
        task_id="task-live-publisher",
        metadata=_current_orca_queue_metadata(
            reaction_dir,
            queue_record_sync_metadata(
                "preparing",
                token="live-token",
                owner_pid=os.getpid(),
            ),
        ),
    )

    with patch.object(publication_mod, "upsert_queued_job_record") as upsert:
        assert publication_mod.repair_queue_publication(cfg, tmp_path, entry)

    upsert.assert_called_once()
    [repaired] = list_queue(tmp_path)
    assert repaired.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE


@pytest.mark.parametrize("changed_state", ["future_v2_marker", QUEUE_RECORD_SYNC_ABORTED])
def test_orca_publication_repair_rejects_invalid_marker_after_lock_reload(
    tmp_path: Path,
    changed_state: str,
) -> None:
    cfg = _make_cfg(str(tmp_path))
    reaction_dir = tmp_path / "rxn"
    entry = enqueue(
        tmp_path,
        str(reaction_dir),
        task_id="task-marker-race",
        metadata=_current_orca_queue_metadata(
            reaction_dir,
            queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_REPAIR_PENDING,
                token="marker-race-token",
                owner_pid=0,
            ),
        ),
    )
    # The marker changes durably in the store after the prefilter read; the
    # repair claim re-reads under the publication lock and must refuse it.
    assert (
        update_metadata(
            tmp_path,
            entry.queue_id,
            {QUEUE_RECORD_SYNC_KEY: changed_state},
        )
        is not None
    )

    with patch.object(publication_mod, "upsert_queued_job_record") as upsert:
        assert not publication_mod.repair_queue_publication(cfg, tmp_path, entry)

    upsert.assert_not_called()


def test_orca_publication_repair_ignores_malformed_terminal_history(tmp_path: Path) -> None:
    cfg = _make_cfg(str(tmp_path))
    terminal = QueueEntry(
        queue_id="terminal-history",
        app_name="orca_auto_orca",
        task_id="terminal-task",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.COMPLETED,
        metadata={
            "reaction_dir": "/outside/history",
            QUEUE_RECORD_SYNC_KEY: "corrupt-terminal-marker",
        },
    )

    with patch.object(publication_mod, "upsert_queued_job_record") as upsert:
        assert publication_mod.repair_queue_publication(cfg, tmp_path, terminal)

    upsert.assert_not_called()


def test_orca_publication_repair_validates_every_selected_input_path(tmp_path: Path) -> None:
    cfg = _make_cfg(str(tmp_path))
    reaction_dir = tmp_path / "rxn"
    entry = enqueue(
        tmp_path,
        str(reaction_dir),
        task_id="task-conflicting-input-paths",
        metadata=_current_orca_queue_metadata(
            reaction_dir,
            {
                "selected_inp": str(reaction_dir / "job.inp"),
                "selected_input_xyz": str(tmp_path.parent / "outside.xyz"),
                **queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_REPAIR_PENDING,
                    token="conflicting-input-token",
                    owner_pid=0,
                ),
            },
        ),
    )

    with patch.object(publication_mod, "upsert_queued_job_record") as upsert:
        assert not publication_mod.repair_queue_publication(cfg, tmp_path, entry)

    upsert.assert_not_called()


def test_orca_publication_repair_fences_crash_row_with_reserved_reaction_dir(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg(str(tmp_path))
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    original_status = reaction_dir.stat()
    entry = enqueue(
        tmp_path,
        str(reaction_dir),
        task_id="task-crashed-publisher",
        metadata={
            "reaction_dir": str(reaction_dir),
            "execution_snapshot": {
                "job_dir_identity": {
                    "device": int(original_status.st_dev),
                    "inode": int(original_status.st_ino),
                }
            },
            **queue_record_sync_metadata(
                "preparing",
                token="crashed-publication-token",
                owner_pid=999_999,
            ),
        },
    )
    # The bound reaction directory keeps its inode but is moved into a reserved
    # ORCA execution generation under the queue root and replaced by a symlink.
    # The production scan filter must fence the crash row rather than re-publish
    # a reserved/unsafe target into the queued index.
    generation_dir = tmp_path / "job" / "20260714-224054-959479f2"
    generation_dir.parent.mkdir(parents=True)
    reaction_dir.rename(generation_dir)
    reaction_dir.symlink_to(generation_dir, target_is_directory=True)

    with patch.object(publication_mod, "upsert_queued_job_record") as upsert:
        assert publication_mod.repair_queue_publication(cfg, tmp_path, entry)

    upsert.assert_not_called()
    [fenced] = list_queue(tmp_path)
    assert fenced.status == QueueStatus.FAILED
    assert fenced.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED
    assert fenced.metadata[TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY] is True
    assert fenced.metadata.get("orca_terminal_replay") is None
    assert fenced.error == "queue_publication_job_dir_invalid:reaction_dir_reserved_or_unsafe"
    assert dequeue_next(tmp_path) is None
