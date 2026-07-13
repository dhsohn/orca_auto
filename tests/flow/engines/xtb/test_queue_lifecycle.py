from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orca_auto.core.queue import lifecycle as _queue_lifecycle
from orca_auto.core.queue.internal_engine import InternalEngineSpec

queue_lifecycle = SimpleNamespace(
    finalize_child_exit=InternalEngineSpec(engine="xtb").lifecycle().finalize_child_exit,
    live_worker_pid_slots=_queue_lifecycle.live_worker_pid_slots,
)


def _append_and_return(items: Any, value: Any, result: Any) -> Any:
    items.append(value)
    return result


def _entry(
    queue_id: str = "queue-1",
    *,
    status: str = "running",
    cancel_requested: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        queue_id=queue_id,
        status=SimpleNamespace(value=status),
        cancel_requested=cancel_requested,
    )


def test_finalize_child_exit_requeues_running_job_and_marks_recovery(tmp_path: Path) -> None:
    cfg = object()
    entry = _entry()
    job = SimpleNamespace(
        queue_root=tmp_path / "queue",
        entry=entry,
        admission_token="slot-1",
    )
    requeued: list[tuple[Path, str]] = []
    recovery: list[tuple[object, object, str]] = []
    released: list[str] = []

    queue_lifecycle.finalize_child_exit(
        cfg,
        job,
        rc=0,
        shutdown_requested=True,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        mark_cancelled_fn=lambda *args, **kwargs: None,
        requeue_running_entry_fn=lambda root, queue_id, **_kwargs: _append_and_return(
            requeued,
            (root, queue_id),
            entry,
        ),
        mark_failed_fn=lambda *args, **kwargs: None,
        mark_recovery_pending_fn=lambda cfg_obj, entry_obj, *, reason: recovery.append(
            (cfg_obj, entry_obj, reason)
        ),
        release_admission_slot_fn=lambda token: released.append(token),
    )

    assert requeued == [(tmp_path / "queue", "queue-1")]
    assert recovery == [(cfg, entry, "worker_shutdown")]
    assert released == ["slot-1"]


def test_finalize_child_exit_skips_recovery_when_requeue_cancels(tmp_path: Path) -> None:
    cfg = object()
    entry = _entry()
    job = SimpleNamespace(
        queue_root=tmp_path / "queue",
        entry=entry,
        admission_token="slot-1",
    )
    requeued: list[tuple[Path, str]] = []
    recovery: list[tuple[object, object, str]] = []
    released: list[str] = []

    def requeue(root: Path, queue_id: str, **_kwargs: object) -> SimpleNamespace:
        requeued.append((root, queue_id))
        return SimpleNamespace(status=SimpleNamespace(value="cancelled"))

    queue_lifecycle.finalize_child_exit(
        cfg,
        job,
        rc=0,
        shutdown_requested=True,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        mark_cancelled_fn=lambda *args, **kwargs: None,
        requeue_running_entry_fn=requeue,
        mark_failed_fn=lambda *args, **kwargs: None,
        mark_recovery_pending_fn=lambda cfg_obj, entry_obj, *, reason: recovery.append(
            (cfg_obj, entry_obj, reason)
        ),
        release_admission_slot_fn=lambda token: released.append(token),
    )

    assert requeued == [(tmp_path / "queue", "queue-1")]
    assert recovery == []
    assert released == ["slot-1"]


def test_finalize_child_exit_marks_cancelled_when_cancel_requested(tmp_path: Path) -> None:
    entry = _entry(cancel_requested=True)
    job = SimpleNamespace(
        queue_root=tmp_path / "queue",
        entry=entry,
        admission_token="slot-1",
    )
    cancelled: list[tuple[Path, str, str]] = []

    def mark_cancelled(root: Path, queue_id: str, *, error: str, **_kwargs: object) -> None:
        cancelled.append((root, queue_id, error))

    queue_lifecycle.finalize_child_exit(
        object(),
        job,
        rc=0,
        shutdown_requested=False,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        mark_cancelled_fn=mark_cancelled,
        requeue_running_entry_fn=lambda *args, **kwargs: None,
        mark_failed_fn=lambda *args, **kwargs: None,
        mark_recovery_pending_fn=lambda *args, **kwargs: None,
        release_admission_slot_fn=lambda _token: None,
    )

    assert cancelled == [(tmp_path / "queue", "queue-1", "cancel_requested")]


def test_finalize_child_exit_marks_failed_on_unexpected_child_exit(tmp_path: Path) -> None:
    entry = _entry()
    job = SimpleNamespace(
        queue_root=tmp_path / "queue",
        entry=entry,
        admission_token="slot-1",
    )
    failed: list[tuple[Path, str, str]] = []

    def mark_failed(root: Path, queue_id: str, *, error: str, **_kwargs: object) -> None:
        failed.append((root, queue_id, error))

    queue_lifecycle.finalize_child_exit(
        object(),
        job,
        rc=9,
        shutdown_requested=False,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        mark_cancelled_fn=lambda *args, **kwargs: None,
        requeue_running_entry_fn=lambda *args, **kwargs: None,
        mark_failed_fn=mark_failed,
        mark_recovery_pending_fn=lambda *args, **kwargs: None,
        release_admission_slot_fn=lambda _token: None,
    )

    assert failed == [(tmp_path / "queue", "queue-1", "worker_child_exit_code=9")]


def test_live_worker_pid_slots_keeps_only_running_live_worker_pids(tmp_path: Path) -> None:
    running = _entry("running")
    invalid_pid = _entry("invalid")
    queued = _entry("queued", status="queued")
    job_dirs = {
        "running": tmp_path / "running",
        "invalid": tmp_path / "invalid",
        "queued": tmp_path / "queued",
    }
    states: dict[str, dict[str, Any]] = {
        "running": {"process": {"worker_pid": "123"}},
        "invalid": {"process": {"worker_pid": "not-a-pid"}},
        "queued": {"process": {"worker_pid": "456"}},
    }

    slots = queue_lifecycle.live_worker_pid_slots(
        [
            (tmp_path / "queue", running),
            (tmp_path / "queue", invalid_pid),
            (tmp_path / "queue", queued),
        ],
        load_state_fn=lambda job_dir: states[Path(job_dir).name],
        job_dir_fn=lambda entry: job_dirs[entry.queue_id],
        pid_is_alive_fn=lambda pid: pid == 123,
    )

    assert [slot.queue_id for slot in slots] == ["running"]
