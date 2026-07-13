from __future__ import annotations

from types import SimpleNamespace

from orca_auto.core.queue.child.process import reconcile_orphaned_child_queue_entries
from orca_auto.core.queue.types import QueueStatus


def test_reconcile_orphaned_child_queue_entries_scopes_live_slots_by_work_dir(
    tmp_path,
) -> None:
    root_a = tmp_path / "queue-a"
    root_b = tmp_path / "queue-b"
    job_a = tmp_path / "job-a"
    job_b = tmp_path / "job-b"
    entry_a = SimpleNamespace(
        queue_id="shared-q",
        task_id="task-a",
        status=QueueStatus.RUNNING,
        cancel_requested=False,
        metadata={"job_dir": str(job_a)},
    )
    entry_b = SimpleNamespace(
        queue_id="shared-q",
        task_id="task-b",
        status=QueueStatus.RUNNING,
        cancel_requested=False,
        metadata={"job_dir": str(job_b)},
    )
    requeued: list[tuple[object, str]] = []
    recovered: list[object] = []

    def requeue(root: object, queue_id: str, **_kwargs: object) -> object:
        requeued.append((root, queue_id))
        return entry_b

    reconcile_orphaned_child_queue_entries(
        SimpleNamespace(),
        admission_root=tmp_path / "admission",
        queue_roots_fn=lambda _cfg: (root_a, root_b),
        list_queue_fn=lambda root: [entry_a] if root == root_a else [entry_b],
        list_slots_fn=lambda _root: [SimpleNamespace(queue_id="shared-q", work_dir=str(job_a))],
        reconcile_stale_slots_fn=lambda _root: None,
        running_status=QueueStatus.RUNNING,
        mark_cancelled_fn=lambda root, queue_id, **_kwargs: None,
        requeue_running_entry_fn=requeue,
        mark_recovery_pending_fn=lambda _cfg, entry: recovered.append(entry),
    )

    assert requeued == [(root_b, "shared-q")]
    assert recovered == [entry_b]


def test_reconcile_orphaned_child_queue_entries_skips_recovery_when_requeue_cancels(
    tmp_path,
) -> None:
    queue_root = tmp_path / "queue"
    job_dir = tmp_path / "job"
    entry = SimpleNamespace(
        queue_id="queue-1",
        task_id="task-1",
        status=QueueStatus.RUNNING,
        cancel_requested=False,
        metadata={"job_dir": str(job_dir)},
    )
    recovered: list[object] = []

    reconcile_orphaned_child_queue_entries(
        SimpleNamespace(),
        admission_root=tmp_path / "admission",
        queue_roots_fn=lambda _cfg: (queue_root,),
        list_queue_fn=lambda _root: [entry],
        list_slots_fn=lambda _root: [],
        reconcile_stale_slots_fn=lambda _root: None,
        running_status=QueueStatus.RUNNING,
        mark_cancelled_fn=lambda root, queue_id, **_kwargs: None,
        requeue_running_entry_fn=lambda root, queue_id, **_kwargs: SimpleNamespace(
            queue_id=queue_id,
            status=QueueStatus.CANCELLED,
        ),
        mark_recovery_pending_fn=lambda _cfg, current: recovered.append(current),
    )

    assert recovered == []


def test_reconcile_orphaned_child_skips_replacement_generation(tmp_path) -> None:
    queue_root = tmp_path / "queue"
    selected = SimpleNamespace(
        queue_id="shared-q",
        task_id="task-a",
        status=QueueStatus.RUNNING,
        cancel_requested=False,
        metadata={"job_dir": str(tmp_path / "job-a")},
    )
    replacement = SimpleNamespace(
        queue_id="shared-q",
        task_id="task-b",
        status=QueueStatus.RUNNING,
        cancel_requested=False,
        metadata={"job_dir": str(tmp_path / "job-b")},
    )
    recovered: list[object] = []

    def requeue(_root, _queue_id, *, expected_entry, **_kwargs):
        return replacement if replacement is expected_entry else None

    reconcile_orphaned_child_queue_entries(
        SimpleNamespace(),
        admission_root=tmp_path / "admission",
        queue_roots_fn=lambda _cfg: (queue_root,),
        list_queue_fn=lambda _root: [selected],
        list_slots_fn=lambda _root: [],
        reconcile_stale_slots_fn=lambda _root: None,
        running_status=QueueStatus.RUNNING,
        mark_cancelled_fn=lambda *_args, **_kwargs: None,
        requeue_running_entry_fn=requeue,
        mark_recovery_pending_fn=lambda _cfg, current: recovered.append(current),
    )

    assert recovered == []
