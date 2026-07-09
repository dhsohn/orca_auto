from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orca_auto.core.queue.types import QueueStatus
from orca_auto.flow.engines.crest import execution as worker_child


def test_run_worker_child_job_processes_loaded_entry_and_releases_slot(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(name="cfg")
    entry = SimpleNamespace(queue_id="queue-1", status=QueueStatus.RUNNING)
    dependencies = object()
    installed: list[Any] = []
    released: list[tuple[str, str]] = []
    processed: list[dict[str, Any]] = []

    def molecule_key_resolver(*_args: Any) -> str:
        return "mol-1"

    rc = worker_child._worker_child.run_worker_child_job(
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
        load_config_fn=lambda _path: cfg,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        admission_root_fn=lambda _cfg: "/tmp/admission",
        release_slot_fn=lambda root, token: released.append((str(root), token)),
        install_signal_handlers_fn=lambda controller: installed.append(controller),
        process_dequeued_entry_fn=lambda *args, **kwargs: processed.append(
            {"args": args, "kwargs": kwargs}
        ),
        dependencies_fn=lambda: dependencies,
        molecule_key_resolver=molecule_key_resolver,
        requeue_running_entry_fn=lambda *_args: None,
        mark_recovery_pending_context_fn=lambda *_args, **_kwargs: None,
        await_parent_admission_handoff_fn=lambda *_args: True,
    )

    assert rc == 0
    assert len(installed) == 1
    assert released == []
    assert processed[0]["args"] == (cfg, entry)
    assert processed[0]["kwargs"]["queue_root"] == (tmp_path / "queue").resolve()
    assert processed[0]["kwargs"]["molecule_key_resolver"] is molecule_key_resolver
    assert processed[0]["kwargs"]["dependencies"] is dependencies
    assert processed[0]["kwargs"]["shutdown_requested"]() is False


def test_run_worker_child_job_injects_default_molecule_key_resolver(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(name="cfg")
    entry = SimpleNamespace(queue_id="queue-1", status=QueueStatus.RUNNING)
    processed: list[dict[str, Any]] = []

    rc = worker_child._worker_child.run_worker_child_job(
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token=None,
        load_config_fn=lambda _path: cfg,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        admission_root_fn=lambda _cfg: "/tmp/admission",
        release_slot_fn=lambda *_args: None,
        install_signal_handlers_fn=lambda _controller: None,
        process_dequeued_entry_fn=lambda *args, **kwargs: processed.append(
            {"args": args, "kwargs": kwargs}
        ),
        dependencies_fn=lambda: object(),
        requeue_running_entry_fn=lambda *_args: None,
        mark_recovery_pending_context_fn=lambda *_args, **_kwargs: None,
        await_parent_admission_handoff_fn=lambda *_args: True,
    )

    assert rc == 0
    assert callable(processed[0]["kwargs"]["molecule_key_resolver"])


def test_run_worker_child_job_requeues_and_marks_recovery_on_shutdown(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(name="cfg")
    entry = SimpleNamespace(queue_id="queue-1", status=QueueStatus.RUNNING)
    context = SimpleNamespace(job_dir=tmp_path / "job")
    requeued: list[tuple[Path, str]] = []
    recovery: list[tuple[object, object, str]] = []
    released: list[tuple[str, str]] = []

    def raise_shutdown(*_args: Any, **_kwargs: Any) -> None:
        raise worker_child.WorkerShutdownRequested(context)

    rc = worker_child._worker_child.run_worker_child_job(
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
        load_config_fn=lambda _path: cfg,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        admission_root_fn=lambda _cfg: "/tmp/admission",
        release_slot_fn=lambda root, token: released.append((str(root), token)),
        install_signal_handlers_fn=lambda _controller: None,
        process_dequeued_entry_fn=raise_shutdown,
        dependencies_fn=lambda: object(),
        molecule_key_resolver=lambda *_args: "mol-1",
        requeue_running_entry_fn=lambda root, queue_id: requeued.append((root, queue_id)),
        mark_recovery_pending_context_fn=lambda cfg_obj, context_obj, *, reason: recovery.append(
            (cfg_obj, context_obj, reason)
        ),
        await_parent_admission_handoff_fn=lambda *_args: True,
    )

    assert rc == 0
    assert requeued == [((tmp_path / "queue").resolve(), "queue-1")]
    assert recovery == [(cfg, context, "worker_shutdown")]
    assert released == []


def test_run_worker_child_job_returns_failure_when_entry_is_not_running(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(name="cfg")
    entry = SimpleNamespace(queue_id="queue-1", status=QueueStatus.PENDING)
    released: list[tuple[str, str]] = []

    rc = worker_child._worker_child.run_worker_child_job(
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
        load_config_fn=lambda _path: cfg,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        admission_root_fn=lambda _cfg: "/tmp/admission",
        release_slot_fn=lambda root, token: released.append((str(root), token)),
        install_signal_handlers_fn=lambda _controller: None,
        process_dequeued_entry_fn=lambda *args, **kwargs: None,
        dependencies_fn=lambda: object(),
        molecule_key_resolver=lambda *_args: "mol-1",
        requeue_running_entry_fn=lambda *_args: None,
        mark_recovery_pending_context_fn=lambda *_args, **_kwargs: None,
        await_parent_admission_handoff_fn=lambda *_args: True,
    )

    assert rc == 1
    assert released == []
