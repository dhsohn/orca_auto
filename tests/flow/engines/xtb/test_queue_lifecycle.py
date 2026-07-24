from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from orca_auto.flow.engines.xtb.engine import ENGINE_DEFINITION

queue_lifecycle = SimpleNamespace(
    finalize_child_exit=ENGINE_DEFINITION.build_queue_runtime().finalize_child_exit,
)


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
