from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue.child import execution as child_execution
from orca_auto.core.queue.child.entrypoint import ChildWorkerEntrypointJob
from orca_auto.core.queue.engine import child as engine_child
from orca_auto.core.queue.engine import execution as engine_execution
from orca_auto.core.queue.internal_engine import InternalEngineSpec


class _WorkerShutdownRequested(RuntimeError):
    def __init__(self, context: Any):
        super().__init__("worker_shutdown")
        self.context = context


def test_build_engine_worker_child_command_supports_admission_root_modes(
    tmp_path: Path,
) -> None:
    with_root = engine_child.WorkerChildCommandSpec("orca_auto.demo.worker_execution")
    without_root = engine_child.WorkerChildCommandSpec(
        "orca_auto.demo_no_root.worker_execution",
        include_admission_root=False,
    )

    with_root_command = engine_child.build_engine_worker_child_command(
        spec=with_root,
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_root="/tmp/admission",
        admission_token="slot-1",
    )
    without_root_command = engine_child.build_engine_worker_child_command(
        spec=without_root,
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-2",
        admission_token="slot-2",
    )

    assert "--admission-root" in with_root_command
    assert "/tmp/admission" in with_root_command
    assert "--admission-root" not in without_root_command
    assert "--admission-token" in without_root_command


def test_run_child_job_with_admission_scope_releases_and_returns_status(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(admission_root=tmp_path / "admission")
    job = ChildWorkerEntrypointJob(
        cfg=cfg,
        queue_root=tmp_path / "queue",
        entry=SimpleNamespace(queue_id="queue-1"),
        _admission_root_fn=lambda loaded_cfg: loaded_cfg.admission_root,
    )
    released: list[tuple[Path, str]] = []

    result = engine_child.run_child_job_with_admission_scope(
        job,
        "slot-1",
        release_slot_fn=lambda root, token: released.append((Path(root), token)),
        run_job_fn=lambda loaded_job: 7 if loaded_job is job else 1,
    )

    assert result == 7
    assert released == []


def test_run_engine_worker_child_job_processes_entry_with_extra_kwargs(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(admission_root=tmp_path / "admission")
    entry = SimpleNamespace(queue_id="queue-1", status="running")
    dependencies = object()
    resolver = object()
    installed: list[Any] = []
    released: list[tuple[Path, str]] = []
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
        release_slot_fn=lambda root, token: released.append((Path(root), token)),
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
    assert released == []
    assert processed[0]["args"] == (cfg, entry)
    assert processed[0]["kwargs"]["queue_root"] == (tmp_path / "queue").resolve()
    assert processed[0]["kwargs"]["molecule_key_resolver"] is resolver
    assert processed[0]["kwargs"]["dependencies"] is dependencies
    assert processed[0]["kwargs"]["shutdown_requested"]() is False


def test_run_engine_worker_child_job_can_map_outcome_to_exit_code(tmp_path: Path) -> None:
    cfg = SimpleNamespace(admission_root=tmp_path / "admission")
    entry = SimpleNamespace(queue_id="queue-1", status="running")
    released: list[tuple[Path, str]] = []

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
        release_slot_fn=lambda root, token: released.append((Path(root), token)),
        install_signal_handlers_fn=lambda _controller: None,
        process_dequeued_entry_fn=lambda *_args, **_kwargs: SimpleNamespace(exit_code=7),
        dependencies_fn=lambda: object(),
        requeue_running_entry_fn=lambda *_args: None,
        mark_recovery_pending_context_fn=lambda *_args, **_kwargs: None,
    )

    assert rc == 7
    assert released == []


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
            release_slot_fn=lambda _root, _token: events.append("child_release"),
            install_signal_handlers_fn=lambda _controller: None,
            process_dequeued_entry_fn=process_entry,
            dependencies_fn=lambda: object(),
            requeue_running_entry_fn=lambda *_args: None,
            mark_recovery_pending_context_fn=lambda *_args, **_kwargs: None,
        )

    assert "terminal_result" not in events
    assert "failure_result" not in events
    assert "child_release" not in events

    parent_job = SimpleNamespace(
        queue_root=tmp_path / "queue",
        entry=entry,
        admission_token="slot-1",
        await_parent_admission_handoff_fn=lambda *_args: True,
    )
    InternalEngineSpec(engine="xtb").lifecycle().finalize_child_exit(
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


def test_internal_engine_worker_child_builds_shutdown_signal_installer() -> None:
    child = InternalEngineSpec(
        engine="demo",
        worker_job_module="orca_auto.demo.worker_execution",
    ).worker_child(_WorkerShutdownRequested)
    controller = child_execution.ChildWorkerShutdownController()
    callbacks: list[Any] = []

    install = child.shutdown_signal_handler_installer(lambda callback: callbacks.append(callback))
    install(controller)

    assert controller.is_requested() is False
    assert len(callbacks) == 1
    callbacks[0]()
    assert controller.is_requested() is True


def test_internal_engine_worker_child_module_facade_keeps_patchable_exports() -> None:
    facade = InternalEngineSpec(
        engine="demo",
        worker_job_module="orca_auto.demo.worker_execution",
        include_admission_root=False,
    ).worker_child_module_facade(
        _WorkerShutdownRequested,
        outcome_exit_code_fn=lambda outcome: int(outcome.exit_code),
        build_worker_child_command=lambda **kwargs: ["worker", kwargs["queue_id"]],
    )

    assert facade.WORKER_JOB_MODULE == "orca_auto.demo.worker_execution"
    assert facade.WorkerShutdownRequested is _WorkerShutdownRequested
    assert facade.build_worker_child_command(
        config_path="/tmp/cfg.yaml",
        queue_root="/tmp/queue",
        queue_id="queue-1",
    ) == ["worker", "queue-1"]
    outcome_exit_code_fn = facade.worker_child.run_spec.outcome_exit_code_fn
    assert outcome_exit_code_fn is not None
    assert outcome_exit_code_fn(SimpleNamespace(exit_code=8)) == 8

    facade.run_worker_child_job = lambda **_kwargs: 5
    assert (
        facade.run_worker_child_job(
            config_path="/tmp/cfg.yaml",
            queue_root="/tmp/queue",
            queue_id="queue-1",
        )
        == 5
    )


def test_run_engine_worker_child_job_requeues_and_marks_recovery_on_shutdown(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(admission_root=tmp_path / "admission")
    entry = SimpleNamespace(queue_id="queue-1", status="running")
    context = SimpleNamespace(job_dir=tmp_path / "job")
    requeued: list[tuple[Path, str]] = []
    recovery: list[tuple[Any, Any, str]] = []
    released: list[tuple[Path, str]] = []

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
        release_slot_fn=lambda root, token: released.append((Path(root), token)),
        install_signal_handlers_fn=lambda _controller: None,
        process_dequeued_entry_fn=raise_shutdown,
        dependencies_fn=lambda: object(),
        requeue_running_entry_fn=lambda root, queue_id: requeued.append((root, queue_id)),
        mark_recovery_pending_context_fn=lambda cfg_obj, context_obj, *, reason: recovery.append(
            (cfg_obj, context_obj, reason)
        ),
    )

    assert rc == 0
    assert requeued == [((tmp_path / "queue").resolve(), "queue-1")]
    assert recovery == [(cfg, context, "worker_shutdown")]
    assert released == []


def test_run_engine_worker_child_job_skips_recovery_when_requeue_cancels(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(admission_root=tmp_path / "admission")
    entry = SimpleNamespace(queue_id="queue-1", status="running")
    context = SimpleNamespace(job_dir=tmp_path / "job")
    requeued: list[tuple[Path, str]] = []
    recovery: list[tuple[Any, Any, str]] = []
    released: list[tuple[Path, str]] = []

    def raise_shutdown(*_args: Any, **_kwargs: Any) -> None:
        raise _WorkerShutdownRequested(context)

    def requeue(root: Path, queue_id: str) -> SimpleNamespace:
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
        release_slot_fn=lambda root, token: released.append((Path(root), token)),
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
    assert released == []


def test_outcome_exit_code_maps_terminal_statuses() -> None:
    assert (
        engine_child.outcome_exit_code(SimpleNamespace(result=SimpleNamespace(status="completed")))
        == 0
    )
    assert (
        engine_child.outcome_exit_code(SimpleNamespace(result=SimpleNamespace(status="cancelled")))
        == 0
    )
    assert (
        engine_child.outcome_exit_code(SimpleNamespace(result=SimpleNamespace(status="failed")))
        == 1
    )
