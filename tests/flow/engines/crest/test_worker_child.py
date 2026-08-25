from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orca_auto.core.queue.types import QueueStatus
from orca_auto.flow.engines.crest import execution as worker_child


def _crest_entry(status: object = QueueStatus.RUNNING, **overrides: Any) -> SimpleNamespace:
    values = {
        "queue_id": "queue-1",
        "app_name": "orca_auto_crest",
        "task_id": "crest-task-1",
        "task_kind": "crest_conformer_search",
        "engine": "crest",
        "status": status,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_crest_worker_child_spec_requires_running_complete_crest_identity() -> None:
    ready = worker_child._WORKER_CHILD_RUN_SPEC.entry_ready_fn
    assert ready is not None
    assert ready(_crest_entry())
    assert not ready(_crest_entry(QueueStatus.PENDING))
    assert not ready(_crest_entry(engine="xtb"))
    assert not ready(_crest_entry(task_kind="conformer_search"))
    assert not ready(_crest_entry(queue_id=""))


def test_run_worker_child_job_wires_canonical_child_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(name="cfg")
    entry = _crest_entry()
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

    def fake_run_engine_worker_child_job(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 17

    monkeypatch.setattr(
        worker_child,
        "run_engine_worker_child_job",
        fake_run_engine_worker_child_job,
    )

    rc = worker_child.run_worker_child_job(
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
        dependencies=dependencies,
    )

    assert rc == 17
    assert captured["spec"] is worker_child._WORKER_CHILD_RUN_SPEC
    assert captured["load_config_fn"]("ignored") is cfg
    assert captured["find_queue_entry_fn"](tmp_path, "queue-1") is entry
    assert captured["dependencies_fn"]() is dependencies
    assert captured["process_dequeued_entry_fn"] is worker_child.process_dequeued_entry
    assert captured["process_dequeued_entry_kwargs"] == {
        "molecule_key_resolver": worker_child._molecule_key
    }
    assert captured["requeue_running_entry_fn"] is worker_child.requeue_running_entry
    assert captured["mark_recovery_pending_context_fn"] is (
        worker_child._mark_recovery_pending_context
    )
