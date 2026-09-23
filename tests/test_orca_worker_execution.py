from __future__ import annotations

import fcntl
from argparse import Namespace
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils import lock as lock_utils
from orca_auto.orca import worker_execution
from orca_auto.orca.config import AppConfig, load_config
from orca_auto.orca.queue import adapter
from orca_auto.orca.queue.terminal_replay import terminal_replay_marker_from_entry
from orca_auto.orca.queue.worker import QueueWorker
from orca_auto.orca.state_reading import load_state
from orca_auto.orca.submission import create_queued_submission


def _queued_submission(tmp_path: Path) -> tuple[AppConfig, Path, QueueEntry, Path]:
    runs_root = tmp_path / "runs"
    job_dir = runs_root / "job"
    job_dir.mkdir(parents=True)
    selected = job_dir / "h2.inp"
    selected.write_text("! HF STO-3G SP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    executable = tmp_path / "fake-orca"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        f"runs_root: {runs_root}\nscheduler:\n  max_active_simulations: 1\n"
        f"orca:\n  paths:\n    orca_executable: {executable}\n"
    )
    cfg = load_config(str(config_path))
    queued = create_queued_submission(
        cfg, Namespace(force=False, priority=10), job_dir, selected_inp=selected
    ).entry
    return cfg, config_path, queued, executable


def test_worker_retains_prelaunch_snapshot_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orca_auto.core.queue.worker import loop

    cfg, config_path, queued, executable = _queued_submission(tmp_path)
    executable.write_text("#!/bin/sh\nexit 91\n")
    monkeypatch.setattr(loop, "install_shutdown_signal_handlers", lambda _callback: None)
    worker = QueueWorker(cfg, str(config_path), max_concurrent=1)
    worker.poll_interval_seconds = 0.01

    assert worker.run_once() == 0

    runs_root = Path(cfg.runtime.allowed_root)
    current = adapter.get_entry_by_id(runs_root, queued.queue_id)
    state = load_state(Path(queued.metadata["reaction_dir"]))
    assert current is not None and current.status is QueueStatus.FAILED
    expected_reason = (
        "execution rejected: Queued engine executable no longer matches its submitted identity"
    )
    assert current.error == expected_reason
    assert state is not None and state["final_result"] is not None
    assert state["final_result"]["reason"] == expected_reason
    assert state["attempts"] == []
    assert terminal_replay_marker_from_entry(current) is None


@pytest.mark.parametrize("transition", ["cancel", "requeue", "redequeue", "replacement"])
def test_prelaunch_rejection_does_not_overwrite_a_changed_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    cfg, config_path, _queued, _executable = _queued_submission(tmp_path)
    runs_root = Path(cfg.runtime.allowed_root)
    running = adapter.dequeue_next(runs_root)
    assert running is not None

    def change_claim_then_reject(*_args: Any, **_kwargs: Any) -> Any:
        if transition == "cancel":
            cancelled = adapter.cancel(runs_root, running.queue_id, expected_entry=running)
            assert cancelled is not None and cancelled.cancel_requested
        elif transition == "replacement":
            # A stale child must not reject another generation, even if its
            # queue ID and dequeue timestamp remain the same.
            queue_store.save_entries(runs_root, [replace(running, task_id="replacement-task")])
        else:
            assert adapter.requeue_running_entry(
                runs_root, running.queue_id, expected_entry=running
            )
            if transition == "redequeue":
                reclaimed = adapter.dequeue_next(runs_root)
                assert reclaimed is not None and reclaimed.started_at != running.started_at
        raise ValueError("Queued engine executable no longer matches its submitted identity")

    monkeypatch.setattr(worker_execution, "_build_execution_context", change_claim_then_reject)

    with pytest.raises(ValueError, match="executable no longer matches"):
        worker_execution.process_dequeued_entry(
            cfg,
            running,
            queue_root=runs_root,
            worker_config_path=str(config_path),
        )

    current = adapter.get_entry_by_id(runs_root, running.queue_id)
    assert current is not None
    assert current.status is (
        QueueStatus.PENDING if transition == "requeue" else QueueStatus.RUNNING
    )
    assert current.cancel_requested is (transition == "cancel")
    assert current.error == ""
    if transition == "replacement":
        assert current.task_id == "replacement-task"


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
    monkeypatch.setattr(queue_store, "load_entries", timed_out_loader)

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

    # The lock's diagnostic payload is stamped after the flock is held, so a
    # clock that raises there simulates a post-acquire failure inside queue_lock.
    def timed_out_payload_clock() -> str:
        raise TimeoutError("simulated lock payload timeout")

    monkeypatch.setattr(
        worker_execution._engine_execution,
        "run_engine_worker_entry_with_spec_factory_options",
        fake_run_engine_worker_entry,
    )
    monkeypatch.setattr(lock_utils, "now_utc_iso", timed_out_payload_clock)

    outcome = worker_execution.process_dequeued_entry(
        object(),
        entry,
        queue_root=tmp_path,
        worker_config_path="/tmp/orca_auto.yaml",
    )

    assert outcome is sentinel
    with pytest.raises(TimeoutError, match="simulated lock payload timeout"):
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

    def timed_out_payload_clock() -> str:
        raise TimeoutError(message)

    monkeypatch.setattr(
        worker_execution._engine_execution,
        "run_engine_worker_entry_with_spec_factory_options",
        fake_run_engine_worker_entry,
    )
    monkeypatch.setattr(lock_utils, "now_utc_iso", timed_out_payload_clock)

    outcome = worker_execution.process_dequeued_entry(
        object(),
        entry,
        queue_root=tmp_path,
        worker_config_path="/tmp/orca_auto.yaml",
    )

    assert outcome is sentinel
    with pytest.raises(TimeoutError, match="Timed out acquiring lock"):
        captured["should_cancel"]()
