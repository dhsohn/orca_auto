"""``orca.queue.roots``: root listing, ORCA identity filtering and the fenced by-id claim."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
)
from orca_auto.core.queue.types import QueueEntry
from orca_auto.orca.config import AppConfig
from orca_auto.orca.queue import roots
from orca_auto.orca.queue.identity import (
    entry_matches_engine_identity,
    own_engine_accept_entry,
)
from tests.conftest import make_app_cfg


def _cfg(tmp_path: Path) -> AppConfig:
    return make_app_cfg(tmp_path / "unused")


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


def _use_roots(monkeypatch: pytest.MonkeyPatch, *queue_roots: Path) -> None:
    monkeypatch.setattr(roots, "runtime_roots_for_cfg", lambda _cfg: tuple(queue_roots))


def test_engine_identity_rejects_conflicting_present_labels() -> None:
    accept_orca = own_engine_accept_entry("orca")

    own_entry = SimpleNamespace(
        queue_id="q-orca",
        app_name="orca_auto_orca",
        task_id="orca-1",
        task_kind="orca_run_inp",
        engine="orca",
        metadata={"job_type": "opt"},
    )
    assert accept_orca(own_entry)

    for overrides in (
        {"app_name": "orca_auto_other"},
        {"engine": "other"},
        {"queue_id": ""},
        {"task_id": ""},
        {"task_kind": ""},
        {"task_kind": "other_run"},
        {"app_name": "", "engine": ""},
    ):
        assert not accept_orca(SimpleNamespace(**{**vars(own_entry), **overrides}))

    assert not entry_matches_engine_identity(
        SimpleNamespace(
            queue_id="q-other-invalid",
            app_name="orca_auto_other",
            task_id="other-1",
            task_kind="other_run",
            engine="other",
        ),
        "other",
    )

    canonical_orca_mapping = {
        "queue_id": "q-orca",
        "app_name": "orca_auto_orca",
        "task_id": "orca-1",
        "task_kind": "orca_run_inp",
        "engine": "orca",
        "metadata": {},
    }
    assert entry_matches_engine_identity(canonical_orca_mapping, "orca")
    for missing_field in ("app_name", "task_id", "task_kind", "engine"):
        partial = dict(canonical_orca_mapping)
        partial.pop(missing_field)
        assert not entry_matches_engine_identity(partial, "orca")


def test_queue_roots_are_the_resolved_runs_root(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    cfg = make_app_cfg(runs_root)

    assert roots.queue_roots(cfg) == (runs_root.resolve(),)
    # Listing never creates a missing root.
    assert roots.existing_queue_roots(cfg) == ()
    assert roots.queue_entries_with_roots(cfg) == []
    assert not runs_root.exists()


def test_queue_roots_propagates_runtime_root_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_roots(_cfg: Any) -> tuple[Path, ...]:
        raise RuntimeError("bad runtime roots")

    monkeypatch.setattr(roots, "runtime_roots_for_cfg", broken_roots)
    with pytest.raises(RuntimeError, match="bad runtime roots"):
        roots.queue_roots(_cfg(tmp_path))


def test_listing_skips_missing_roots_and_foreign_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    own_entry = _internal_entry("orca", "own")
    foreign_entry = _internal_entry("other", "foreign")
    seen: list[Path] = []

    def list_queue(root: Path) -> list[Any]:
        seen.append(root)
        return [foreign_entry, own_entry]

    _use_roots(monkeypatch, tmp_path / "missing", queue_root)
    monkeypatch.setattr(roots, "list_queue", list_queue)

    assert roots.queue_entries_with_roots(_cfg(tmp_path)) == [(queue_root, own_entry)]
    assert seen == [queue_root]
    assert not (tmp_path / "missing").exists()


def test_peek_preserves_selection_without_dequeuing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    foreign_entry = _internal_entry("other", "foreign")
    own_entry = replace(_internal_entry("orca", "own"), priority=-1)
    fallback_entry = replace(_internal_entry("orca", "fallback"), priority=5)
    seen: list[Path] = []

    def list_queue(root: Path) -> list[Any]:
        seen.append(root)
        return {root_a: [foreign_entry, own_entry], root_b: [fallback_entry]}[root]

    def unexpected_dequeue(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("preview must not dequeue a row")

    _use_roots(monkeypatch, tmp_path / "missing", root_a, root_b)
    monkeypatch.setattr(roots, "list_queue", list_queue)
    monkeypatch.setattr(roots, "dequeue_entry_if_pending", unexpected_dequeue)

    assert roots.peek_next_entry(_cfg(tmp_path)) == (root_a, own_entry)
    # The first root with a claimable row wins; only one runtime root exists.
    assert seen == [root_a]
    assert own_entry.status.value == fallback_entry.status.value == "pending"


def test_dequeue_claims_the_previewed_row_by_id_fenced_on_that_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    winner = replace(_internal_entry("orca", "winner"), priority=1)
    later = replace(_internal_entry("orca", "same-root-later"), priority=9)
    foreign = replace(_internal_entry("other", "foreign"), priority=0)
    claimed: list[tuple[Path, str, Any]] = []

    def dequeue_by_id(claim_root: Path, queue_id: str, *, expected_entry: Any) -> Any:
        claimed.append((claim_root, queue_id, expected_entry))
        return expected_entry

    _use_roots(monkeypatch, root)
    monkeypatch.setattr(roots, "list_queue", lambda _root: [foreign, later, winner])
    monkeypatch.setattr(roots, "dequeue_entry_if_pending", dequeue_by_id)

    assert roots.peek_next_entry(_cfg(tmp_path)) == (root, winner)
    assert roots.dequeue_next_entry(_cfg(tmp_path)) == (root, winner)
    assert claimed == [(root, "winner", winner)]


def test_dequeue_returns_none_when_the_previewed_row_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    _use_roots(monkeypatch, root)
    monkeypatch.setattr(roots, "list_queue", lambda _root: [_internal_entry("orca", "pending")])
    monkeypatch.setattr(roots, "dequeue_entry_if_pending", lambda *_args, **_kwargs: None)

    assert roots.dequeue_next_entry(_cfg(tmp_path)) is None


def test_skip_predicate_steers_both_the_preview_and_the_by_id_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    tracked = _internal_entry("orca", "queue-tracked")
    behind = _internal_entry("orca", "queue-behind")
    foreign = _internal_entry("other", "queue-foreign")
    claimed: list[tuple[str, Any]] = []

    def dequeue_by_id(_root: Path, queue_id: str, *, expected_entry: Any) -> Any:
        claimed.append((queue_id, expected_entry))
        return expected_entry

    _use_roots(monkeypatch, root)
    monkeypatch.setattr(roots, "list_queue", lambda _root: [foreign, tracked, behind])
    monkeypatch.setattr(roots, "dequeue_entry_if_pending", dequeue_by_id)

    def skip(entry: Any) -> bool:
        # The engine filter runs first, so a foreign row never reaches it.
        assert entry is not foreign
        return entry is tracked

    cfg = _cfg(tmp_path)
    assert roots.peek_next_entry(cfg, skip_entry_fn=skip) == (root, behind)
    assert roots.dequeue_next_entry(cfg, skip_entry_fn=skip) == (root, behind)
    assert claimed == [("queue-behind", behind)]
    assert roots.peek_next_entry(cfg, skip_entry_fn=lambda _entry: True) is None
    assert roots.peek_next_entry(cfg) == (root, tracked)


def test_by_id_lookup_reports_a_foreign_row_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own_entry = _internal_entry("orca", "queue-own")
    foreign_entry = _internal_entry("other", "queue-foreign")
    looked_up: list[tuple[Path, str]] = []

    def get_entry_by_id(root: Path, queue_id: str) -> Any | None:
        looked_up.append((root, queue_id))
        return own_entry if queue_id == own_entry.queue_id else foreign_entry

    monkeypatch.setattr(roots, "get_entry_by_id", get_entry_by_id)

    assert roots.queue_entry_by_id(tmp_path, own_entry.queue_id) is own_entry
    assert roots.queue_entry_by_id(str(tmp_path), foreign_entry.queue_id) is None
    assert looked_up == [(tmp_path, own_entry.queue_id), (tmp_path, foreign_entry.queue_id)]
