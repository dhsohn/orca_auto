from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.engines.queue_worker import (
    EngineQueueWorker,
    EngineWorkerPolicy,
    build_runtime_engine_queue_worker,
)


def _cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(tmp_path / "runs"),
            admission_root=str(tmp_path / "admission"),
            admission_limit=1,
            max_concurrent=1,
            resolved_admission_root=str(tmp_path / "admission"),
            resolved_admission_limit=1,
        )
    )


def _deps(calls: list[str]) -> SimpleNamespace:
    def reserve_dequeued_entry(_cfg: Any, **_kwargs: Any) -> tuple[str, None]:
        calls.append("reserve_dequeued_entry")
        return "idle", None

    return SimpleNamespace(
        poll_interval_seconds=1,
        time=SimpleNamespace(sleep=lambda _seconds: None),
        admission_root=lambda cfg: cfg.runtime.resolved_admission_root,
        release_slot=lambda _root, _token: None,
        reserve_dequeued_entry=reserve_dequeued_entry,
        has_admission_capacity=lambda _cfg: True,
        peek_next_entry=lambda _cfg: None,
        dequeue_next_entry=lambda _cfg: None,
        start_background_job_process=lambda **_kwargs: None,
        try_reserve_admission_slot=lambda _cfg: None,
    )


def _hooks(calls: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        reconcile_worker_state=lambda _worker: calls.append("hooks.reconcile_worker_state"),
        finalize_completed_job=lambda _worker, _queue_id, _job, rc: calls.append(
            f"hooks.finalize_completed_job:{rc}"
        ),
    )


def _worker(
    tmp_path: Path, calls: list[str], policy: EngineWorkerPolicy | None
) -> EngineQueueWorker:
    return build_runtime_engine_queue_worker(
        _cfg(tmp_path),
        config_path="",
        default_config_path=lambda: str(tmp_path / "orca_auto.yaml"),
        engine="demo",
        max_concurrent=1,
        deps=_deps(calls),
        hooks=_hooks(calls),
        worker_pid_file_name="demo_worker.pid",
        admission_root=tmp_path / "admission",
        policy=policy,
    )


def test_policy_is_immutable_and_optional_by_step() -> None:
    policy = EngineWorkerPolicy(after_init=lambda worker: None)

    assert policy.reserve_gate is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.reserve_gate = lambda worker: None  # type: ignore[misc]


def test_worker_runs_after_init_once_with_itself(tmp_path: Path) -> None:
    seen: list[Any] = []
    calls: list[str] = []
    after_init = seen.append

    worker = _worker(tmp_path, calls, EngineWorkerPolicy(after_init=after_init))

    assert seen == [worker]
    assert worker.policy.after_init is after_init
    assert worker.engine == "demo"


def test_reserve_gate_short_circuits_admission_only_when_it_answers(tmp_path: Path) -> None:
    calls: list[str] = []
    answers: list[tuple[str, None] | None] = [("blocked", None), None]
    worker = _worker(
        tmp_path,
        calls,
        EngineWorkerPolicy(reserve_gate=lambda _worker: answers.pop(0)),
    )

    assert worker._reserve_next_entry() == ("blocked", None)
    assert calls == []
    assert worker._reserve_next_entry() == ("idle", None)
    assert calls == ["reserve_dequeued_entry"]


def test_worker_without_a_policy_uses_shared_defaults(tmp_path: Path) -> None:
    calls: list[str] = []

    worker = _worker(tmp_path, calls, None)

    assert worker.policy == EngineWorkerPolicy()
    assert worker._reserve_next_entry() == ("idle", None)
    assert worker._running_queue_id(SimpleNamespace(queue_id="q-1")) == "q-1"
    with pytest.raises(AttributeError, match="finalize_child_exit"):
        worker._finalize_child_exit(SimpleNamespace(), rc=0)
    worker._finalize_finished_job("q-1", SimpleNamespace(), rc=2)
    worker._reconcile_orphaned_running()
    worker._check_cancel_requests()

    assert calls == [
        "reserve_dequeued_entry",
        "hooks.finalize_completed_job:2",
        "hooks.reconcile_worker_state",
    ]


def test_interrupt_and_job_factory_steps_receive_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Any]] = []
    worker = _worker(
        tmp_path,
        [],
        EngineWorkerPolicy(
            keyboard_interrupt=lambda w: calls.append(("interrupt", w is worker)),
            running_job_factory=lambda w, **kwargs: calls.append(("job", (w is worker, kwargs))),
        ),
    )

    def interrupt() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "_check_completed_jobs", interrupt)
    with pytest.raises(KeyboardInterrupt):
        worker._run_iteration()
    entry = SimpleNamespace(queue_id="q-1")
    worker._make_running_job(queue_root=tmp_path, entry=entry, process=None, admission_token="tok")

    assert calls == [
        ("interrupt", True),
        (
            "job",
            (
                True,
                {"queue_root": tmp_path, "entry": entry, "process": None, "admission_token": "tok"},
            ),
        ),
    ]


def test_policy_steps_receive_the_worker_and_the_job(tmp_path: Path) -> None:
    calls: list[tuple[str, Any]] = []
    job = SimpleNamespace(queue_id="q-1")
    worker = _worker(
        tmp_path,
        [],
        EngineWorkerPolicy(
            running_queue_id=lambda entry: f"id:{entry.queue_id}",
            finalize_finished_job=lambda w, queue_id, current, *, rc: calls.append(
                ("finished", (w is worker, queue_id, current is job, rc))
            ),
            finalize_child_exit=lambda w, current, *, rc: calls.append(
                ("exit", (w is worker, current is job, rc))
            ),
            reconcile_orphaned_running=lambda w: calls.append(("reconcile", w is worker)),
            check_cancel_requests=lambda w: calls.append(("cancel", w is worker)),
        ),
    )

    assert worker._running_queue_id(job) == "id:q-1"
    worker._finalize_finished_job("q-1", job, rc=3)
    worker._finalize_child_exit(job, rc=4)
    worker._reconcile_orphaned_running()
    worker._check_cancel_requests()

    assert calls == [
        ("finished", (True, "q-1", True, 3)),
        ("exit", (True, True, 4)),
        ("reconcile", True),
        ("cancel", True),
    ]
