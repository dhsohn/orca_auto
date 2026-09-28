from __future__ import annotations

import fcntl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.admission import admission_dir, get_slot, reserve_slot
from orca_auto.core.queue import persistence as queue_persistence
from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils import lock as lock_utils
from orca_auto.orca import worker_execution
from orca_auto.orca.orca_runner import WorkerShutdownInterrupt
from orca_auto.orca.queue import adapter
from orca_auto.orca.queue import entries as queue_entries
from orca_auto.orca.queue.terminal_marker import terminal_replay_marker_from_entry
from orca_auto.orca.queue.worker import OrcaQueueWorker
from orca_auto.orca.state_reading import load_state
from tests.conftest import claim_next_entry
from tests.queue_worker_helpers import queued_submission


def test_worker_retains_prelaunch_snapshot_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orca_auto.core.queue.worker import loop

    cfg, config_path, queued, executable = queued_submission(tmp_path)
    executable.write_text("#!/bin/sh\nexit 91\n")
    monkeypatch.setattr(loop, "install_shutdown_signal_handlers", lambda _callback: None)
    worker = OrcaQueueWorker(cfg, str(config_path))
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
    cfg, config_path, _queued, _executable = queued_submission(tmp_path)
    runs_root = Path(cfg.runtime.allowed_root)
    running = claim_next_entry(runs_root)
    assert running is not None

    def change_claim_then_reject(*_args: Any, **_kwargs: Any) -> Any:
        if transition == "cancel":
            cancelled = adapter.cancel(runs_root, running.queue_id, expected_entry=running)
            assert cancelled is not None and cancelled.cancel_requested
        elif transition == "replacement":
            # A stale child must not reject another generation, even if its
            # queue ID and dequeue timestamp remain the same.
            queue_persistence.save_entries(
                runs_root, [replace(running, task_id="replacement-task")]
            )
        else:
            assert adapter.requeue_running_entry(
                runs_root, running.queue_id, expected_entry=running
            )
            if transition == "redequeue":
                reclaimed = claim_next_entry(runs_root)
                assert reclaimed is not None and reclaimed.started_at != running.started_at
        raise ValueError("Queued engine executable no longer matches its submitted identity")

    monkeypatch.setattr(worker_execution, "_build_execution_context", change_claim_then_reject)

    with pytest.raises(ValueError, match="executable no longer matches"):
        worker_execution.process_dequeued_entry(
            cfg,
            running,
            queue_root=runs_root,
            admission_token="",
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
    cfg, _config_path, entry, _executable = queued_submission(tmp_path)
    captured: dict[str, Any] = {}
    lock_calls: list[tuple[Path, float]] = []
    flock_calls: list[int] = []
    original_file_lock = queue_store.file_lock

    def fake_execute(*_args: object, **kwargs: Any) -> int:
        captured.update(kwargs)
        return 4

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

    monkeypatch.setattr(worker_execution, "execute_orca_run", fake_execute)
    monkeypatch.setattr(queue_store, "file_lock", recording_file_lock)
    monkeypatch.setattr(lock_utils.fcntl, "flock", contended_flock)

    exit_code = worker_execution.process_dequeued_entry(
        cfg,
        entry,
        queue_root=tmp_path,
        admission_token="",
    )

    assert exit_code == 4
    assert captured["stop_requested"]() is False
    assert lock_calls == [(tmp_path.resolve() / queue_store.QUEUE_LOCK_NAME, 0.0)]
    assert flock_calls == [fcntl.LOCK_EX | fcntl.LOCK_NB]


def test_child_cancellation_probe_propagates_non_lock_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg, _config_path, entry, _executable = queued_submission(tmp_path)
    captured: dict[str, Any] = {}

    def fake_execute(*_args: object, **kwargs: Any) -> int:
        captured.update(kwargs)
        return 4

    def timed_out_loader(_root: Path) -> list[object]:
        raise TimeoutError("simulated queue payload timeout")

    monkeypatch.setattr(worker_execution, "execute_orca_run", fake_execute)
    monkeypatch.setattr(queue_store, "load_entries", timed_out_loader)

    exit_code = worker_execution.process_dequeued_entry(
        cfg,
        entry,
        queue_root=tmp_path,
        admission_token="",
    )

    assert exit_code == 4
    with pytest.raises(TimeoutError, match="simulated queue payload timeout"):
        captured["stop_requested"]()


def test_child_cancellation_probe_propagates_post_acquire_payload_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg, _config_path, entry, _executable = queued_submission(tmp_path)
    captured: dict[str, Any] = {}

    def fake_execute(*_args: object, **kwargs: Any) -> int:
        captured.update(kwargs)
        return 4

    # The lock's diagnostic payload is stamped after the flock is held, so a
    # clock that raises there simulates a post-acquire failure inside queue_lock.
    def timed_out_payload_clock() -> str:
        raise TimeoutError("simulated lock payload timeout")

    monkeypatch.setattr(worker_execution, "execute_orca_run", fake_execute)
    monkeypatch.setattr(lock_utils, "now_utc_iso", timed_out_payload_clock)

    exit_code = worker_execution.process_dequeued_entry(
        cfg,
        entry,
        queue_root=tmp_path,
        admission_token="",
    )

    assert exit_code == 4
    with pytest.raises(TimeoutError, match="simulated lock payload timeout"):
        captured["stop_requested"]()


def test_child_cancellation_probe_propagates_post_acquire_timeout_with_lock_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg, _config_path, entry, _executable = queued_submission(tmp_path)
    captured: dict[str, Any] = {}
    message = f"Timed out acquiring lock: {tmp_path.resolve() / queue_store.QUEUE_LOCK_NAME}"

    def fake_execute(*_args: object, **kwargs: Any) -> int:
        captured.update(kwargs)
        return 4

    def timed_out_payload_clock() -> str:
        raise TimeoutError(message)

    monkeypatch.setattr(worker_execution, "execute_orca_run", fake_execute)
    monkeypatch.setattr(lock_utils, "now_utc_iso", timed_out_payload_clock)

    exit_code = worker_execution.process_dequeued_entry(
        cfg,
        entry,
        queue_root=tmp_path,
        admission_token="",
    )

    assert exit_code == 4
    with pytest.raises(TimeoutError, match="Timed out acquiring lock"):
        captured["stop_requested"]()


@pytest.mark.parametrize("boundary", ["handoff", "shutdown", "cancel", "exception"])
def test_child_retains_parent_reservation_at_execution_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    cfg, config_path, queued, _executable = queued_submission(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    admission_root = admission_dir(cfg.runtime.allowed_root)
    running = claim_next_entry(queue_root)
    assert running is not None
    token = reserve_slot(admission_root, 1, source="orca-child-boundary-test")
    assert token is not None
    slot_before = get_slot(admission_root, token)
    assert slot_before is not None
    handoffs: list[tuple[Path, str]] = []
    executions: list[str] = []

    def handoff(root: str | Path, admission_token: str) -> bool:
        handoffs.append((Path(root), admission_token))
        return boundary != "handoff"

    def install_signal_handler(callback: Callable[[], None]) -> None:
        if boundary == "shutdown":
            callback()

    def execute(*_args: Any, **_kwargs: Any) -> int:
        executions.append(boundary)
        if boundary == "cancel":
            cancelled = adapter.cancel(queue_root, queued.queue_id, expected_entry=running)
            assert cancelled is not None and cancelled.cancel_requested
            raise WorkerShutdownInterrupt
        if boundary == "exception":
            raise RuntimeError("execution failed before terminal publication")
        pytest.fail("child launched without handoff or after shutdown")

    monkeypatch.setattr(
        worker_execution, "install_shutdown_signal_handlers", install_signal_handler
    )
    monkeypatch.setattr(worker_execution, "execute_orca_run", execute)

    def run_child() -> int:
        return worker_execution.run_worker_child_job(
            config_path=str(config_path),
            queue_root=queue_root,
            queue_id=queued.queue_id,
            admission_token=token,
            await_parent_admission_handoff_fn=handoff,
        )

    if boundary == "exception":
        with pytest.raises(RuntimeError, match="before terminal publication"):
            run_child()
    else:
        assert run_child() == (1 if boundary == "handoff" else 0)

    current = adapter.get_entry_by_id(queue_root, queued.queue_id)
    assert current is not None
    expected = {
        "handoff": QueueStatus.RUNNING,
        "shutdown": QueueStatus.PENDING,
        "cancel": QueueStatus.CANCELLED,
        "exception": QueueStatus.RUNNING,
    }
    assert current.status is expected[boundary]
    assert current.metadata["execution_snapshot"] == running.metadata["execution_snapshot"]
    assert get_slot(admission_root, token) == slot_before
    assert handoffs == [(admission_root, token)]
    assert executions == ([boundary] if boundary in {"cancel", "exception"} else [])


def _running_state_for(rxn: Path, task_id: str) -> None:
    from orca_auto.orca.state import new_state, save_state

    state = new_state(rxn, rxn / "h2.inp")
    state["job_id"] = task_id
    state["status"] = "running"
    save_state(rxn, state)


def _run_cancelled_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, QueueEntry, Callable[[], int]]:
    cfg, config_path, queued, _executable = queued_submission(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    admission_root = admission_dir(cfg.runtime.allowed_root)
    running = claim_next_entry(queue_root)
    assert running is not None
    token = reserve_slot(admission_root, 1, source="orca-child-cancel-lock-test")
    assert token is not None
    rxn = Path(queue_entries.queue_entry_reaction_dir(running))
    _running_state_for(rxn, queued.task_id)

    def execute(*_args: Any, **_kwargs: Any) -> int:
        cancelled = adapter.cancel(queue_root, queued.queue_id, expected_entry=running)
        assert cancelled is not None and cancelled.cancel_requested
        raise WorkerShutdownInterrupt

    monkeypatch.setattr(worker_execution, "install_shutdown_signal_handlers", lambda _cb: None)
    monkeypatch.setattr(worker_execution, "execute_orca_run", execute)

    def run_child() -> int:
        return worker_execution.run_worker_child_job(
            config_path=str(config_path),
            queue_root=queue_root,
            queue_id=queued.queue_id,
            admission_token=token,
            await_parent_admission_handoff_fn=lambda _root, _token: True,
        )

    return rxn, queue_root, queued, run_child


def test_cancelled_child_leaves_the_cancelled_result_to_the_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orca_auto.orca.queue import settlement

    rxn, queue_root, queued, run_child = _run_cancelled_child(tmp_path, monkeypatch)

    assert run_child() == 0

    # The child wrote no terminal result; it only marked its row cancelled
    # with the replay marker, which observed the running state.
    untouched = load_state(rxn)
    assert untouched is not None
    assert untouched["status"] == "running"
    assert not isinstance(untouched.get("final_result"), dict)
    row = adapter.get_entry_by_id(queue_root, queued.queue_id)
    assert row is not None and row.status is QueueStatus.CANCELLED
    marker = terminal_replay_marker_from_entry(row)
    assert marker is not None
    assert (marker["observed_state"]["job_id"], marker["observed_state"]["terminal_status"]) == (
        queued.task_id,
        "",
    )

    # The parent's settlement of that row is the one writer of the result.
    item = settlement.work_item_for_row(queue_root, row)
    assert item is not None
    prepared = settlement.prepare(item)
    settled = load_state(rxn)
    assert settled is not None
    settled_result = settled["final_result"]
    assert settled_result is not None
    assert (settled["status"], settled_result["status"], settled_result["reason"]) == (
        "cancelled",
        "cancelled",
        "cancel_requested",
    )
    assert (prepared.resolved_status, prepared.run_id) == ("cancelled", settled["run_id"])
