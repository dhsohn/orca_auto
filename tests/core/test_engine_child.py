from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.engines import worker_child
from orca_auto.core.queue.engine import child as engine_child
from orca_auto.core.queue.engine import execution as engine_execution
from orca_auto.core.queue.engine.runtime import EngineQueueRuntime


def _append_and_return(items: Any, value: Any, result: Any) -> Any:
    items.append(value)
    return result


class _WorkerShutdownRequested(RuntimeError):
    def __init__(self, context: Any):
        super().__init__("worker_shutdown")
        self.context = context


def test_run_engine_worker_child_job_processes_entry_with_extra_kwargs(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(admission_root=tmp_path / "admission")
    entry = SimpleNamespace(queue_id="queue-1", task_id="task-1", status="running")
    dependencies = object()
    resolver = object()
    installed: list[Any] = []
    processed: list[dict[str, Any]] = []

    rc = engine_child.run_engine_worker_child_job(
        spec=engine_child.WorkerChildRunSpec(
            shutdown_exception_type=_WorkerShutdownRequested,
            entry_ready_fn=lambda loaded_entry: loaded_entry.status == "running",
        ),
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
        await_parent_admission_handoff_fn=lambda *_args: True,
        load_config_fn=lambda _path: cfg,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        admission_root_fn=lambda loaded_cfg: loaded_cfg.admission_root,
        install_signal_handlers_fn=lambda controller: installed.append(controller),
        process_dequeued_entry_fn=lambda *args, **kwargs: processed.append(
            {"args": args, "kwargs": kwargs}
        ),
        dependencies_fn=lambda: dependencies,
        requeue_running_entry_fn=lambda *_args: None,
        mark_recovery_pending_context_fn=lambda *_args, **_kwargs: None,
        process_dequeued_entry_kwargs={"molecule_key_resolver": resolver},
    )

    assert rc == 0
    assert len(installed) == 1
    assert processed[0]["args"] == (cfg, entry)
    assert processed[0]["kwargs"]["queue_root"] == (tmp_path / "queue").resolve()
    assert processed[0]["kwargs"]["molecule_key_resolver"] is resolver
    assert processed[0]["kwargs"]["dependencies"] is dependencies
    assert processed[0]["kwargs"]["shutdown_requested"]() is False


def test_run_engine_worker_child_job_can_map_outcome_to_exit_code(tmp_path: Path) -> None:
    cfg = SimpleNamespace(admission_root=tmp_path / "admission")
    entry = SimpleNamespace(queue_id="queue-1", task_id="task-1", status="running")

    rc = engine_child.run_engine_worker_child_job(
        spec=engine_child.WorkerChildRunSpec(
            shutdown_exception_type=_WorkerShutdownRequested,
            entry_ready_fn=lambda loaded_entry: loaded_entry.status == "running",
            outcome_exit_code_fn=lambda outcome: outcome.exit_code,
        ),
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
        await_parent_admission_handoff_fn=lambda *_args: True,
        load_config_fn=lambda _path: cfg,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        admission_root_fn=lambda loaded_cfg: loaded_cfg.admission_root,
        install_signal_handlers_fn=lambda _controller: None,
        process_dequeued_entry_fn=lambda *_args, **_kwargs: SimpleNamespace(exit_code=7),
        dependencies_fn=lambda: object(),
        requeue_running_entry_fn=lambda *_args: None,
        mark_recovery_pending_context_fn=lambda *_args, **_kwargs: None,
    )

    assert rc == 7


def test_cleanup_failure_keeps_child_and_parent_admission_until_engine_exit(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(admission_root=tmp_path / "admission")
    entry = SimpleNamespace(
        queue_id="queue-1",
        status=SimpleNamespace(value="running"),
        cancel_requested=False,
    )
    events: list[str] = []

    class EngineProcess:
        exited = False

        def poll(self) -> int | None:
            return 9 if self.exited else None

    process = EngineProcess()

    def sleep(_seconds: float) -> None:
        events.append("cleanup_wait")
        if events.count("cleanup_wait") == 3:
            process.exited = True
            events.append("engine_exit")

    def process_entry(*_args: Any, **_kwargs: Any) -> Any:
        return engine_execution.run_cancellable_engine_process(
            start_job=lambda: SimpleNamespace(process=process),
            finalize_job=lambda *_args, **_kwargs: events.append("terminal_result"),
            terminate_process=lambda _process: False,
            build_failure_result=lambda _exc: events.append("failure_result"),
            should_cancel=lambda: True,
            sleep=sleep,
            check_cancel_before_poll=True,
            cleanup_retry_attempts=2,
            cleanup_poll_interval_seconds=0,
        )

    with pytest.raises(engine_execution.ProcessCleanupError):
        engine_child.run_engine_worker_child_job(
            spec=engine_child.WorkerChildRunSpec(
                shutdown_exception_type=_WorkerShutdownRequested,
                entry_ready_fn=lambda loaded_entry: loaded_entry.status.value == "running",
            ),
            config_path="/tmp/orca_auto.yaml",
            queue_root=tmp_path / "queue",
            queue_id="queue-1",
            admission_token="slot-1",
            await_parent_admission_handoff_fn=lambda *_args: True,
            load_config_fn=lambda _path: cfg,
            find_queue_entry_fn=lambda _root, _queue_id: entry,
            admission_root_fn=lambda loaded_cfg: loaded_cfg.admission_root,
            install_signal_handlers_fn=lambda _controller: None,
            process_dequeued_entry_fn=process_entry,
            dependencies_fn=lambda: object(),
            requeue_running_entry_fn=lambda *_args: None,
            mark_recovery_pending_context_fn=lambda *_args, **_kwargs: None,
        )

    assert "terminal_result" not in events
    assert "failure_result" not in events

    parent_job = SimpleNamespace(
        queue_root=tmp_path / "queue",
        entry=entry,
        admission_token="slot-1",
        await_parent_admission_handoff_fn=lambda *_args: True,
    )
    runtime = EngineQueueRuntime(
        load_config=lambda value: value,
        runtime_roots_for_cfg=lambda _cfg: (tmp_path / "queue",),
        list_queue=lambda _root: [entry],
        dequeue_next=lambda _root: entry,
        worker_pid_file_name="engine_worker.pid",
    )
    runtime.finalize_child_exit(
        cfg,
        parent_job,
        rc=1,
        shutdown_requested=False,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        mark_cancelled_fn=lambda *_args, **_kwargs: events.append("parent_cancelled"),
        requeue_running_entry_fn=lambda *_args, **_kwargs: events.append("parent_requeued"),
        mark_failed_fn=lambda *_args, **_kwargs: events.append("parent_failed"),
        mark_recovery_pending_fn=lambda *_args, **_kwargs: events.append("parent_recovery"),
        release_admission_slot_fn=lambda _token: events.append("parent_release"),
    )

    assert events.index("engine_exit") < events.index("parent_failed")
    assert events.index("engine_exit") < events.index("parent_release")


def test_run_engine_worker_child_job_requeues_and_marks_recovery_on_shutdown(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(admission_root=tmp_path / "admission")
    entry = SimpleNamespace(queue_id="queue-1", status="running")
    context = SimpleNamespace(job_dir=tmp_path / "job")
    requeued: list[tuple[Path, str]] = []
    recovery: list[tuple[Any, Any, str]] = []

    def raise_shutdown(*_args: Any, **_kwargs: Any) -> None:
        raise _WorkerShutdownRequested(context)

    rc = engine_child.run_engine_worker_child_job(
        spec=engine_child.WorkerChildRunSpec(
            shutdown_exception_type=_WorkerShutdownRequested,
            entry_ready_fn=lambda loaded_entry: loaded_entry.status == "running",
        ),
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
        await_parent_admission_handoff_fn=lambda *_args: True,
        load_config_fn=lambda _path: cfg,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        admission_root_fn=lambda loaded_cfg: loaded_cfg.admission_root,
        install_signal_handlers_fn=lambda _controller: None,
        process_dequeued_entry_fn=raise_shutdown,
        dependencies_fn=lambda: object(),
        requeue_running_entry_fn=lambda root, queue_id, **_kwargs: _append_and_return(
            requeued,
            (root, queue_id),
            entry,
        ),
        mark_recovery_pending_context_fn=lambda cfg_obj, context_obj, *, reason: recovery.append(
            (cfg_obj, context_obj, reason)
        ),
    )

    assert rc == 0
    assert requeued == [((tmp_path / "queue").resolve(), "queue-1")]
    assert recovery == [(cfg, context, "worker_shutdown")]


def test_run_engine_worker_child_job_skips_recovery_when_requeue_cancels(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(admission_root=tmp_path / "admission")
    entry = SimpleNamespace(queue_id="queue-1", task_id="task-1", status="running")
    context = SimpleNamespace(job_dir=tmp_path / "job")
    requeued: list[tuple[Path, str]] = []
    recovery: list[tuple[Any, Any, str]] = []

    def raise_shutdown(*_args: Any, **_kwargs: Any) -> None:
        raise _WorkerShutdownRequested(context)

    def requeue(root: Path, queue_id: str, **_kwargs: object) -> SimpleNamespace:
        requeued.append((root, queue_id))
        return SimpleNamespace(status=SimpleNamespace(value="cancelled"))

    rc = engine_child.run_engine_worker_child_job(
        spec=engine_child.WorkerChildRunSpec(
            shutdown_exception_type=_WorkerShutdownRequested,
            entry_ready_fn=lambda loaded_entry: loaded_entry.status == "running",
        ),
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
        await_parent_admission_handoff_fn=lambda *_args: True,
        load_config_fn=lambda _path: cfg,
        find_queue_entry_fn=lambda _root, _queue_id: entry,
        admission_root_fn=lambda loaded_cfg: loaded_cfg.admission_root,
        install_signal_handlers_fn=lambda _controller: None,
        process_dequeued_entry_fn=raise_shutdown,
        dependencies_fn=lambda: object(),
        requeue_running_entry_fn=requeue,
        mark_recovery_pending_context_fn=lambda cfg_obj, context_obj, *, reason: recovery.append(
            (cfg_obj, context_obj, reason)
        ),
    )

    assert rc == 0
    assert requeued == [((tmp_path / "queue").resolve(), "queue-1")]
    assert recovery == []


def test_worker_child_parser_preserves_spawned_entrypoint_contract() -> None:
    args = worker_child.build_parser().parse_args(
        [
            "--engine",
            "crest",
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

    assert args.engine == "crest"
    assert args.config == "/tmp/orca_auto.yaml"
    assert args.queue_root == "/tmp/queue"
    assert args.queue_id == "queue-1"
    assert args.admission_token == "slot-1"


def test_worker_child_parser_requires_the_engine_it_dispatches_on() -> None:
    with pytest.raises(SystemExit):
        worker_child.build_parser().parse_args(
            [
                "--config",
                "/tmp/orca_auto.yaml",
                "--queue-root",
                "/tmp/queue",
                "--queue-id",
                "queue-1",
            ]
        )


def test_worker_child_main_dispatches_the_parsed_queue_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_engine_worker_child_job(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 37

    monkeypatch.setattr(
        worker_child,
        "run_engine_worker_child_job",
        fake_run_engine_worker_child_job,
    )

    result = worker_child.main(
        [
            "--engine",
            "xtb",
            "--config",
            "/tmp/orca_auto.yaml",
            "--queue-root",
            "/tmp/queue",
            "--queue-id",
            "q-1",
            "--admission-token",
            " slot-1 ",
        ]
    )

    assert result == 37
    assert captured["engine"] == "xtb"
    assert captured["config_path"] == "/tmp/orca_auto.yaml"
    assert captured["queue_root"] == "/tmp/queue"
    assert captured["queue_id"] == "q-1"
    assert captured["admission_token"] == "slot-1"


def test_worker_child_main_treats_a_blank_admission_token_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        worker_child,
        "run_engine_worker_child_job",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    worker_child.main(
        [
            "--engine",
            "orca",
            "--config",
            "/tmp/orca_auto.yaml",
            "--queue-root",
            "/tmp/queue",
            "--queue-id",
            "q-1",
            "--admission-token",
            "   ",
        ]
    )

    assert captured["admission_token"] is None
