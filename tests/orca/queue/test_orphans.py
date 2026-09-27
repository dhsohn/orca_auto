"""``orca_auto.orca.queue.orphans``: one locked queue write per reconciliation pass."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.persistence import queue_lock_path
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils.lock import held_file_lock_payload
from orca_auto.orca.queue.orphans import reconcile_orphaned_running_entries
from tests.conftest import enqueue_entry, make_queue_entry


def _record_saves(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> list[tuple[list[QueueEntry], bool]]:
    saves: list[tuple[list[QueueEntry], bool]] = []
    real_save = queue_store.save_entries

    def save(save_root: Path, entries: Sequence[QueueEntry]) -> None:
        saves.append((list(entries), held_file_lock_payload(queue_lock_path(root)) is not None))
        real_save(save_root, entries)

    monkeypatch.setattr(queue_store, "save_entries", save)
    return saves


def test_reconcile_requeues_every_orphan_in_one_locked_save(
    queue_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, error in (("q-a", "worker lost"), ("q-b", "")):
        enqueue_entry(
            queue_root,
            make_queue_entry(
                queue_id=name,
                reaction_dir=queue_root / name,
                status=QueueStatus.RUNNING,
                started_at="2026-03-10T00:00:00+00:00",
                error=error,
            ),
        )
    saves = _record_saves(monkeypatch, queue_root)

    assert reconcile_orphaned_running_entries(queue_root, ignore_worker_pid=True) == 2

    [(saved, lock_held)] = saves
    assert lock_held
    assert [(row.queue_id, row.status, row.started_at, row.error) for row in saved] == [
        ("q-a", QueueStatus.PENDING, "", "worker lost"),
        ("q-b", QueueStatus.PENDING, "", ""),
    ]
    assert queue_store.list_queue(queue_root) == saved


def test_reconcile_without_orphans_does_not_write(
    queue_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enqueue_entry(
        queue_root,
        make_queue_entry(queue_id="q-pending", reaction_dir=queue_root / "q-pending"),
    )
    saves = _record_saves(monkeypatch, queue_root)

    assert reconcile_orphaned_running_entries(queue_root, ignore_worker_pid=True) == 0
    assert saves == []
