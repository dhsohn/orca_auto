from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orca_auto.core.queue.types import QueueStatus
from orca_auto.flow.engines.crest.engine import ENGINE_DEFINITION


def _append_and_return(items: Any, value: Any, result: Any) -> Any:
    items.append(value)
    return result


queue_lifecycle = SimpleNamespace(
    finalize_child_exit=ENGINE_DEFINITION.build_queue_runtime().finalize_child_exit,
)


def _entry(
    queue_id: str = "queue-1",
    *,
    status: QueueStatus = QueueStatus.RUNNING,
    cancel_requested: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        queue_id=queue_id,
        status=status,
        cancel_requested=cancel_requested,
    )


def test_finalize_child_exit_marks_failed_for_unexpected_child_exit(tmp_path: Path) -> None:
    entry = _entry()
    job = SimpleNamespace(
        queue_root=tmp_path / "queue",
        entry=entry,
        admission_token="slot-1",
    )
    failed: list[tuple[Path, str, str]] = []
    released: list[str] = []

    def mark_failed(root: Path, queue_id: str, *, error: str, **_kwargs: object) -> None:
        failed.append((root, queue_id, error))

    queue_lifecycle.finalize_child_exit(
        object(),
        job,
        rc=7,
        shutdown_requested=False,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        mark_cancelled_fn=lambda *args, **kwargs: None,
        requeue_running_entry_fn=lambda *args, **kwargs: None,
        mark_failed_fn=mark_failed,
        mark_recovery_pending_fn=lambda *args, **kwargs: None,
        release_admission_slot_fn=lambda token: released.append(token),
    )

    assert failed == [(tmp_path / "queue", "queue-1", "worker_child_exit_code=7")]
    assert released == ["slot-1"]


def test_finalize_child_exit_requeues_on_worker_shutdown(tmp_path: Path) -> None:
    cfg = object()
    entry = _entry()
    job_entry = _entry("original")
    job = SimpleNamespace(
        queue_root=tmp_path / "queue",
        entry=job_entry,
        admission_token="slot-1",
    )
    requeued: list[tuple[Path, str]] = []
    recovery: list[tuple[object, object, str]] = []

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
        release_admission_slot_fn=lambda _token: None,
    )

    assert requeued == [(tmp_path / "queue", "queue-1")]
    assert recovery == [(cfg, job_entry, "worker_shutdown")]


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
        shutdown_requested=True,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        mark_cancelled_fn=mark_cancelled,
        requeue_running_entry_fn=lambda *args, **kwargs: None,
        mark_failed_fn=lambda *args, **kwargs: None,
        mark_recovery_pending_fn=lambda *args, **kwargs: None,
        release_admission_slot_fn=lambda _token: None,
    )

    assert cancelled == [(tmp_path / "queue", "queue-1", "cancel_requested")]
