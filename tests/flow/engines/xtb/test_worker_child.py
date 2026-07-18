from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orca_auto.core.queue.child.execution import ChildWorkerShutdownController
from orca_auto.core.queue.types import QueueStatus
from orca_auto.flow.engines.xtb import execution as worker_child


def _xtb_entry(status: object = QueueStatus.RUNNING, **overrides: Any) -> SimpleNamespace:
    values = {
        "queue_id": "queue-1",
        "app_name": "orca_auto_xtb",
        "task_id": "xtb-task-1",
        "task_kind": "xtb_opt",
        "engine": "xtb",
        "status": status,
        "metadata": {"job_type": "opt"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_xtb_worker_child_spec_requires_running_complete_xtb_identity() -> None:
    ready = worker_child._WORKER_CHILD_RUN_SPEC.entry_ready_fn
    assert ready is not None
    assert ready(_xtb_entry())
    assert not ready(_xtb_entry(QueueStatus.PENDING))
    assert not ready(_xtb_entry(engine="crest"))
    assert not ready(_xtb_entry(task_kind="xtb_sp"))
    assert not ready(_xtb_entry(metadata={"job_type": "sp"}))
    assert not ready(_xtb_entry(queue_id=""))


def test_run_worker_job_wires_canonical_child_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(name="cfg")
    entry = _xtb_entry()
    released: list[tuple[str, str]] = []
    dependencies = cast(
        worker_child.WorkerExecutionDependencies,
        SimpleNamespace(
            config=SimpleNamespace(
                load_config=lambda _path: cfg,
                queue_entry_by_id=lambda _root, _queue_id: entry,
            ),
            admission=SimpleNamespace(
                release_slot=lambda root, token: released.append((str(root), token)),
            ),
        ),
    )
    captured: dict[str, Any] = {}
    installed: list[Any] = []

    monkeypatch.setattr(
        worker_child,
        "install_shutdown_signal_handlers",
        lambda callback: installed.append(callback),
    )

    def handoff(*_args: Any) -> bool:
        return True

    def fake_run_engine_worker_child_job(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 17

    monkeypatch.setattr(
        worker_child,
        "run_engine_worker_child_job",
        fake_run_engine_worker_child_job,
    )

    rc = worker_child.run_worker_job(
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
        dependencies=dependencies,
        await_parent_admission_handoff_fn=handoff,
    )

    assert rc == 17
    assert captured["spec"] is worker_child._WORKER_CHILD_RUN_SPEC
    assert captured["load_config_fn"]("ignored") is cfg
    assert captured["find_queue_entry_fn"](tmp_path, "queue-1") is entry
    assert captured["release_slot_fn"] is dependencies.admission.release_slot
    assert captured["dependencies_fn"]() is dependencies
    assert captured["process_dequeued_entry_fn"] is worker_child.process_dequeued_entry
    assert captured["requeue_running_entry_fn"] is worker_child.requeue_running_entry
    assert captured["mark_recovery_pending_context_fn"] is (
        worker_child._mark_recovery_pending_context
    )
    assert captured["await_parent_admission_handoff_fn"] is handoff
    controller = ChildWorkerShutdownController()
    captured["install_signal_handlers_fn"](controller)
    assert not controller.is_requested()
    installed[0]()
    assert controller.is_requested()


def test_worker_child_parser_preserves_entrypoint_contract() -> None:
    args = worker_child.build_parser().parse_args(
        [
            "--config",
            "/tmp/orca_auto.yaml",
            "--queue-root",
            "/tmp/queue",
            "--queue-id",
            "queue-1",
            "--admission-token",
            "slot-1",
        ]
    )

    assert args.config == "/tmp/orca_auto.yaml"
    assert args.queue_root == "/tmp/queue"
    assert args.queue_id == "queue-1"
    assert args.admission_token == "slot-1"
