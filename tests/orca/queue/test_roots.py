"""``orca.queue.roots``: the queue root, its ORCA listing and the fenced by-id claim."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.queue import persistence as queue_persistence
from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
)
from orca_auto.core.queue.types import QueueEntry
from orca_auto.orca.config import AppConfig
from orca_auto.orca.queue import roots
from tests.conftest import make_app_cfg


def _cfg(tmp_path: Path) -> AppConfig:
    return make_app_cfg(tmp_path)


def _internal_entry(engine: str, queue_id: str) -> QueueEntry:
    task_kind = {"orca": "orca_run_inp"}.get(engine, f"{engine}_run")
    return QueueEntry(
        queue_id=queue_id,
        app_name=f"orca_auto_{engine}",
        task_id=f"{engine}-task-{queue_id}",
        task_kind=task_kind,
        engine=engine,
        metadata={QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_COMPLETE},
    )


def test_queue_root_is_the_resolved_runs_root(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    cfg = make_app_cfg(runs_root)

    assert roots.queue_root(cfg) == runs_root.resolve()
    # Listing and previewing never create a missing root.
    assert roots.list_orca_rows(cfg) == []
    assert roots.peek_next_entry(cfg) is None
    assert not runs_root.exists()


def test_listing_holds_only_orca_rows(tmp_path: Path) -> None:
    own_entry = _internal_entry("orca", "own")
    queue_persistence.save_entries(tmp_path, [_internal_entry("other", "foreign"), own_entry])

    assert roots.list_orca_rows(_cfg(tmp_path)) == [own_entry]


def test_peek_preserves_selection_without_dequeuing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own_entry = replace(_internal_entry("orca", "own"), priority=1)
    fallback_entry = replace(_internal_entry("orca", "fallback"), priority=5)
    foreign_entry = replace(_internal_entry("other", "foreign"), priority=0)
    queue_persistence.save_entries(tmp_path, [fallback_entry, foreign_entry, own_entry])

    def unexpected_dequeue(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("preview must not dequeue a row")

    monkeypatch.setattr(roots, "dequeue_entry_if_pending", unexpected_dequeue)

    assert roots.peek_next_entry(_cfg(tmp_path)) == own_entry
    assert [entry.status.value for entry in queue_store.list_queue(tmp_path)] == ["pending"] * 3


def test_dequeue_claims_the_previewed_row_by_id_fenced_on_that_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    winner = replace(_internal_entry("orca", "winner"), priority=1)
    later = replace(_internal_entry("orca", "later"), priority=9)
    foreign = replace(_internal_entry("other", "foreign"), priority=0)
    queue_persistence.save_entries(tmp_path, [foreign, later, winner])
    claimed: list[tuple[Path, str, Any]] = []

    def dequeue_by_id(claim_root: Path, queue_id: str, *, expected_entry: Any) -> Any:
        claimed.append((claim_root, queue_id, expected_entry))
        return expected_entry

    monkeypatch.setattr(roots, "dequeue_entry_if_pending", dequeue_by_id)

    assert roots.peek_next_entry(_cfg(tmp_path)) == winner
    assert roots.dequeue_next_entry(_cfg(tmp_path)) == (tmp_path, winner)
    assert claimed == [(tmp_path, "winner", winner)]


def test_dequeue_returns_none_when_the_previewed_row_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_persistence.save_entries(tmp_path, [_internal_entry("orca", "pending")])
    monkeypatch.setattr(roots, "dequeue_entry_if_pending", lambda *_args, **_kwargs: None)

    assert roots.dequeue_next_entry(_cfg(tmp_path)) is None


def test_skip_predicate_steers_both_the_preview_and_the_by_id_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracked = _internal_entry("orca", "queue-tracked")
    behind = _internal_entry("orca", "queue-behind")
    foreign = _internal_entry("other", "queue-foreign")
    queue_persistence.save_entries(tmp_path, [foreign, tracked, behind])
    claimed: list[tuple[str, Any]] = []

    def dequeue_by_id(_root: Path, queue_id: str, *, expected_entry: Any) -> Any:
        claimed.append((queue_id, expected_entry))
        return expected_entry

    monkeypatch.setattr(roots, "dequeue_entry_if_pending", dequeue_by_id)

    def skip(entry: Any) -> bool:
        # The listing holds ORCA rows only, so a foreign row never reaches it.
        assert entry.queue_id != foreign.queue_id
        return bool(entry.queue_id == tracked.queue_id)

    cfg = _cfg(tmp_path)
    assert roots.peek_next_entry(cfg, skip_entry_fn=skip) == behind
    assert roots.dequeue_next_entry(cfg, skip_entry_fn=skip) == (tmp_path, behind)
    assert claimed == [("queue-behind", behind)]
    assert roots.peek_next_entry(cfg, skip_entry_fn=lambda _entry: True) is None
    assert roots.peek_next_entry(cfg) == tracked
