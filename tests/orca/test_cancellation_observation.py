from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.core.artifacts import QUEUE_FILE
from orca_auto.core.queue import persistence, store
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.app_ids import ORCA_AUTO_ORCA_APP_NAME, ORCA_ENGINE, ORCA_TASK_KIND
from orca_auto.orca.config import AppConfig
from orca_auto.orca.queue import adapter


def entry(queue_id: str = "target", *, cancel_requested: bool = False) -> QueueEntry:
    return QueueEntry(
        queue_id=queue_id,
        app_name=ORCA_AUTO_ORCA_APP_NAME,
        task_id=queue_id,
        task_kind=ORCA_TASK_KIND,
        engine=ORCA_ENGINE,
        status=QueueStatus.RUNNING,
        cancel_requested=cancel_requested,
    )


def test_child_reuses_unchanged_queue_and_detects_atomic_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = entry()
    rows = [entry(str(i)) for i in range(2000)] + [target]
    persistence.save_entries(tmp_path, rows)
    reads = 0
    original = store.load_entries

    def load(root: Path) -> list[QueueEntry]:
        nonlocal reads
        reads += 1
        return original(root)

    monkeypatch.setattr(store, "load_entries", load)
    probe = adapter.cancellation_probe(tmp_path, target)
    for _ in range(10):
        assert not probe()
    assert reads == 1
    rows[-1] = replace(target, cancel_requested=True)
    persistence.save_entries(tmp_path, rows)
    assert probe()
    assert probe()
    assert reads == 2
    rows[-1] = replace(rows[-1], task_id="successor")
    persistence.save_entries(tmp_path, rows)
    assert not probe()
    assert reads == 3


def test_changed_queue_lock_timeout_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = entry()
    persistence.save_entries(tmp_path, [target])
    probe = adapter.cancellation_probe(tmp_path, target)
    assert not probe()
    persistence.save_entries(tmp_path, [replace(target, cancel_requested=True)])

    @contextmanager
    def unavailable(*_args: object, **_kwargs: object):
        raise store.QueueLockTimeoutError("busy")
        yield

    with monkeypatch.context() as patcher:
        patcher.setattr(store, "queue_lock", unavailable)
        with pytest.raises(store.QueueLockTimeoutError):
            probe()
    assert probe()


def test_observation_signature_is_captured_before_unlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = entry()
    persistence.save_entries(tmp_path, [target])
    probe = adapter.cancellation_probe(tmp_path, target)
    original = store.queue_lock

    @contextmanager
    def commit_on_unlock(root: str | Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
        with original(root, timeout_seconds=timeout_seconds):
            yield
        persistence.save_entries(tmp_path, [replace(target, cancel_requested=True)])

    with monkeypatch.context() as patcher:
        patcher.setattr(store, "queue_lock", commit_on_unlock)
        assert not probe()
    assert probe()


def test_removed_and_corrupt_queue_are_not_stale_cache_hits(tmp_path: Path) -> None:
    target = entry(cancel_requested=True)
    persistence.save_entries(tmp_path, [target])
    probe = adapter.cancellation_probe(tmp_path, target)
    assert probe()
    path = tmp_path / QUEUE_FILE
    path.unlink()
    assert not probe()
    path.write_text("{bad json")
    with pytest.raises(store.QueueStoreCorruptError):
        probe()
    persistence.save_entries(tmp_path, [target])
    assert probe()


def test_batch_cancellation_is_one_snapshot_and_preserves_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [entry(str(i), cancel_requested=True) for i in range(4)]
    rows[1] = replace(rows[1], task_id="successor")
    rows[2] = replace(rows[2], engine="other")
    persistence.save_entries(tmp_path, rows)
    reads = []
    original = store.load_entries

    def load(root: Path) -> list[QueueEntry]:
        reads.append(root)
        return original(root)

    monkeypatch.setattr(store, "load_entries", load)
    assert adapter.cancel_requested_ids(tmp_path, {str(i): str(i) for i in range(4)}) == {"0", "3"}
    assert len(reads) == 1


def test_stat_failure_does_not_replace_a_cached_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = entry()
    persistence.save_entries(tmp_path, [target])
    probe = adapter.cancellation_probe(tmp_path, target)
    assert not probe()
    persistence.save_entries(tmp_path, [replace(target, cancel_requested=True)])
    with monkeypatch.context() as patcher:

        def denied(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("stat denied")

        patcher.setattr(Path, "stat", denied)
        with pytest.raises(PermissionError, match="stat denied"):
            probe()
    assert probe()


def test_child_execution_reuses_one_probe_for_all_runner_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orca_auto.orca import worker_execution

    target = entry()
    persistence.save_entries(tmp_path, [target])
    original = store.load_entries
    reads = []

    def load(root: Path) -> list[QueueEntry]:
        reads.append(root)
        return original(root)

    monkeypatch.setattr(store, "load_entries", load)

    def execution(*_args: object, stop_requested: Callable[[], bool], **_kwargs: object) -> int:
        callback = stop_requested
        for _ in range(10):
            assert not callback()
        persistence.save_entries(tmp_path, [replace(target, cancel_requested=True)])
        assert callback()
        return 0

    monkeypatch.setattr(worker_execution, "execute_orca_run", execution)
    monkeypatch.setattr(
        worker_execution,
        "_build_execution_context",
        lambda *_a, **_k: SimpleNamespace(reaction_dir=str(tmp_path)),
    )
    worker_execution.process_dequeued_entry(
        AppConfig(), target, queue_root=tmp_path, admission_token=""
    )
    assert len(reads) == 2
