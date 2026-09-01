from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue import store as queue_store
from orca_auto.core.utils import lock as lock_utils
from orca_auto.orca import worker_execution
from orca_auto.orca.queue import adapter as queue_adapter


def test_child_cancellation_probe_skips_contended_queue_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entry = SimpleNamespace(queue_id="queue-1")
    sentinel = object()
    captured: dict[str, Any] = {}
    lock_calls: list[tuple[Path, float]] = []
    flock_calls: list[int] = []
    original_file_lock = queue_store.file_lock

    def fake_run_engine_worker_entry(*_args: object, **kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    @contextmanager
    def recording_file_lock(
        lock_path: Path,
        *,
        timeout_seconds: float = 10.0,
    ) -> Iterator[None]:
        lock_calls.append((lock_path, timeout_seconds))
        with original_file_lock(lock_path, timeout_seconds=timeout_seconds):
            yield

    def contended_flock(_descriptor: int, operation: int) -> None:
        flock_calls.append(operation)
        raise BlockingIOError

    monkeypatch.setattr(
        worker_execution._engine_execution,
        "run_engine_worker_entry_with_spec_factory_options",
        fake_run_engine_worker_entry,
    )
    monkeypatch.setattr(queue_store, "file_lock", recording_file_lock)
    monkeypatch.setattr(lock_utils.fcntl, "flock", contended_flock)

    outcome = worker_execution.process_dequeued_entry(
        object(),
        entry,
        queue_root=tmp_path,
        worker_config_path="/tmp/orca_auto.yaml",
    )

    assert outcome is sentinel
    assert captured["should_cancel"]() is False
    assert lock_calls == [(tmp_path.resolve() / queue_store.QUEUE_LOCK_NAME, 0.0)]
    assert flock_calls == [fcntl.LOCK_EX | fcntl.LOCK_NB]


def test_child_cancellation_probe_propagates_non_lock_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entry = SimpleNamespace(queue_id="queue-1")
    sentinel = object()
    captured: dict[str, Any] = {}

    def fake_run_engine_worker_entry(*_args: object, **kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    def timed_out_loader(_root: Path) -> list[object]:
        raise TimeoutError("simulated queue payload timeout")

    monkeypatch.setattr(
        worker_execution._engine_execution,
        "run_engine_worker_entry_with_spec_factory_options",
        fake_run_engine_worker_entry,
    )
    monkeypatch.setattr(queue_adapter, "_load_entries", timed_out_loader)

    outcome = worker_execution.process_dequeued_entry(
        object(),
        entry,
        queue_root=tmp_path,
        worker_config_path="/tmp/orca_auto.yaml",
    )

    assert outcome is sentinel
    with pytest.raises(TimeoutError, match="simulated queue payload timeout"):
        captured["should_cancel"]()


def test_child_cancellation_probe_propagates_post_acquire_payload_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entry = SimpleNamespace(queue_id="queue-1")
    sentinel = object()
    captured: dict[str, Any] = {}

    def fake_run_engine_worker_entry(*_args: object, **kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    def timed_out_fsync(_descriptor: int) -> None:
        raise TimeoutError("simulated lock payload fsync timeout")

    monkeypatch.setattr(
        worker_execution._engine_execution,
        "run_engine_worker_entry_with_spec_factory_options",
        fake_run_engine_worker_entry,
    )
    monkeypatch.setattr(lock_utils.os, "fsync", timed_out_fsync)

    outcome = worker_execution.process_dequeued_entry(
        object(),
        entry,
        queue_root=tmp_path,
        worker_config_path="/tmp/orca_auto.yaml",
    )

    assert outcome is sentinel
    with pytest.raises(TimeoutError, match="simulated lock payload fsync timeout"):
        captured["should_cancel"]()


def test_child_cancellation_probe_propagates_post_acquire_timeout_with_lock_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entry = SimpleNamespace(queue_id="queue-1")
    sentinel = object()
    captured: dict[str, Any] = {}
    message = f"Timed out acquiring lock: {tmp_path.resolve() / queue_store.QUEUE_LOCK_NAME}"

    def fake_run_engine_worker_entry(*_args: object, **kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    def timed_out_fsync(_descriptor: int) -> None:
        raise TimeoutError(message)

    monkeypatch.setattr(
        worker_execution._engine_execution,
        "run_engine_worker_entry_with_spec_factory_options",
        fake_run_engine_worker_entry,
    )
    monkeypatch.setattr(lock_utils.os, "fsync", timed_out_fsync)

    outcome = worker_execution.process_dequeued_entry(
        object(),
        entry,
        queue_root=tmp_path,
        worker_config_path="/tmp/orca_auto.yaml",
    )

    assert outcome is sentinel
    with pytest.raises(TimeoutError, match="Timed out acquiring lock"):
        captured["should_cancel"]()
