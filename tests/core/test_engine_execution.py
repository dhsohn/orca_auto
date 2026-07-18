from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue.engine import execution as engine_execution


def test_object_attribute_fields_extracts_named_context_values() -> None:
    context = SimpleNamespace(
        job_type="ranking",
        reaction_key="rxn-1",
        input_summary={"candidate_count": 3},
    )

    assert engine_execution.object_attribute_fields(
        context,
        "job_type",
        "reaction_key",
        "input_summary",
    ) == {
        "job_type": "ranking",
        "reaction_key": "rxn-1",
        "input_summary": {"candidate_count": 3},
    }


def test_build_terminal_result_from_context_merges_identity_and_timestamp(
    tmp_path: Path,
) -> None:
    entry = SimpleNamespace(queue_id="queue-1", started_at="")
    context = SimpleNamespace(
        entry=entry,
        job_dir=tmp_path / "job",
        selected_xyz=tmp_path / "job" / "input.xyz",
        resource_request={"max_cores": 2},
    )
    captured: dict[str, Any] = {}

    def build_terminal_result(entry_obj: Any, **kwargs: Any) -> Any:
        captured["entry"] = entry_obj
        captured["kwargs"] = kwargs
        return "terminal-result"

    result = engine_execution.build_terminal_result_from_context(
        build_terminal_result,
        context,
        identity_fields={"mode": "nci"},
        status="failed",
        reason="runner_error:boom",
        now_utc_iso="2026-01-01T00:00:00+00:00",
    )

    assert result == "terminal-result"
    assert captured["entry"] is entry
    assert captured["kwargs"]["job_dir"] == tmp_path / "job"
    assert captured["kwargs"]["selected_xyz"] == tmp_path / "job" / "input.xyz"
    assert captured["kwargs"]["resource_request"] == {"max_cores": 2}
    assert captured["kwargs"]["mode"] == "nci"
    assert captured["kwargs"]["status"] == "failed"
    assert captured["kwargs"]["reason"] == "runner_error:boom"
    assert captured["kwargs"]["exit_code"] == 1
    assert captured["kwargs"]["now_utc_iso_fn"]() == "2026-01-01T00:00:00+00:00"


def test_run_engine_worker_lifecycle_stops_on_shutdown_before_mark_running(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(tmp_path / "allowed")))
    calls: list[str] = []

    def raise_shutdown(_context: Any) -> None:
        calls.append("check")
        raise RuntimeError("shutdown")

    def build_context(_cfg: Any, _entry: Any) -> SimpleNamespace:
        calls.append("build")
        return SimpleNamespace()

    lifecycle = engine_execution.EngineWorkerLifecycle(
        build_context=build_context,
        check_shutdown=raise_shutdown,
        mark_running=lambda *_args: calls.append("mark"),
        run_job=lambda *_args: calls.append("run"),
        finalize_entry=lambda *_args: calls.append("finalize"),
        build_outcome=lambda *_args: calls.append("outcome"),
    )

    try:
        engine_execution.run_engine_worker_lifecycle(
            cfg,
            SimpleNamespace(queue_id="q-1"),
            queue_root=None,
            lifecycle=lifecycle,
        )
    except RuntimeError as exc:
        assert str(exc) == "shutdown"
    else:
        raise AssertionError("expected shutdown")

    assert calls == ["build", "check"]


def test_run_engine_worker_lifecycle_stops_on_shutdown_after_mark_running(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(tmp_path / "allowed")))
    checks = iter([False, True])
    calls: list[str] = []

    def maybe_raise_shutdown(_context: Any) -> None:
        calls.append("check")
        if next(checks):
            raise RuntimeError("shutdown")

    def build_context(_cfg: Any, _entry: Any) -> SimpleNamespace:
        calls.append("build")
        return SimpleNamespace()

    lifecycle = engine_execution.EngineWorkerLifecycle(
        build_context=build_context,
        check_shutdown=maybe_raise_shutdown,
        mark_running=lambda *_args: calls.append("mark"),
        run_job=lambda *_args: calls.append("run"),
        finalize_entry=lambda *_args: calls.append("finalize"),
        build_outcome=lambda *_args: calls.append("outcome"),
    )

    try:
        engine_execution.run_engine_worker_lifecycle(
            cfg,
            SimpleNamespace(queue_id="q-1"),
            queue_root=None,
            lifecycle=lifecycle,
        )
    except RuntimeError as exc:
        assert str(exc) == "shutdown"
    else:
        raise AssertionError("expected shutdown")

    assert calls == ["build", "check", "mark", "check"]


def test_run_engine_worker_entry_with_adapter_passes_worker_options(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(tmp_path / "allowed")))
    entry = SimpleNamespace(queue_id="q-1")
    calls: list[tuple[str, Any]] = []

    def run_job(_cfg: Any, _context: Any, queue_root: Path, _options: Any) -> str:
        calls.append(("run", queue_root))
        return "result"

    def finalize_entry(
        _cfg: Any,
        _context: Any,
        _result: str,
        _queue_root: Path,
        options: Any,
    ) -> str:
        calls.append(("finalize", options.emit_output))
        return "finalized"

    def build_outcome(_context: Any, result: str, finalized: str) -> str:
        calls.append(("outcome", result))
        return finalized

    adapter = engine_execution.EngineWorkerAdapter(
        build_context=lambda cfg_obj, entry_obj: SimpleNamespace(
            cfg=cfg_obj,
            entry=entry_obj,
        ),
        check_shutdown=lambda context, options: calls.append(("check", options.worker_job_pid)),
        mark_running=lambda cfg_obj, context, options: calls.append(
            ("mark", options.worker_job_pid)
        ),
        run_job=run_job,
        finalize_entry=finalize_entry,
        build_outcome=build_outcome,
    )

    outcome = engine_execution.run_engine_worker_entry_with_adapter(
        cfg,
        entry,
        queue_root=tmp_path / "queue",
        adapter=adapter,
        options=engine_execution.EngineWorkerOptions(
            worker_job_pid=4242,
            emit_output=True,
        ),
    )

    assert outcome == "finalized"
    assert calls == [
        ("check", 4242),
        ("mark", 4242),
        ("check", 4242),
        ("run", tmp_path / "queue"),
        ("finalize", True),
        ("outcome", "result"),
    ]


def test_build_engine_worker_adapter_installs_shutdown_check(tmp_path: Path) -> None:
    class ShutdownRequested(RuntimeError):
        def __init__(self, context: Any) -> None:
            super().__init__("shutdown")
            self.context = context

    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(tmp_path / "allowed")))
    entry = SimpleNamespace(queue_id="q-1")
    context = SimpleNamespace(entry=entry)
    adapter = engine_execution.build_engine_worker_adapter(
        build_context=lambda _cfg, _entry: context,
        mark_running=lambda *_args: None,
        run_job=lambda *_args: "result",
        finalize_entry=lambda *_args: "finalized",
        shutdown_exception_type=ShutdownRequested,
    )

    try:
        engine_execution.run_engine_worker_entry_with_adapter(
            cfg,
            entry,
            queue_root=tmp_path / "queue",
            adapter=adapter,
            options=engine_execution.EngineWorkerOptions(
                shutdown_requested=lambda: True,
            ),
        )
    except ShutdownRequested as exc:
        assert exc.context is context
    else:
        raise AssertionError("expected shutdown")


def test_run_engine_worker_entry_with_hooks_builds_adapter(tmp_path: Path) -> None:
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(tmp_path / "allowed")))
    entry = SimpleNamespace(queue_id="q-1")
    calls: list[tuple[str, Any]] = []

    def run_job(_cfg: Any, _context: Any, queue_root: Path, _options: Any) -> str:
        calls.append(("run", queue_root))
        return "result"

    def finalize_entry(
        _cfg: Any,
        _context: Any,
        result: str,
        _queue_root: Path,
        _options: Any,
    ) -> str:
        calls.append(("finalize", result))
        return "finalized"

    def build_outcome(_context: Any, result: str, finalized: str) -> str:
        calls.append(("outcome", result))
        return finalized

    hooks = engine_execution.EngineWorkerHooks(
        build_context=lambda cfg_obj, entry_obj: SimpleNamespace(
            cfg=cfg_obj,
            entry=entry_obj,
        ),
        mark_running=lambda _cfg, _context, options: calls.append(("mark", options.emit_output)),
        run_job=run_job,
        finalize_entry=finalize_entry,
        build_outcome=build_outcome,
        shutdown_exception_type=RuntimeError,
    )

    outcome = engine_execution.run_engine_worker_entry_with_hooks(
        cfg,
        entry,
        queue_root=tmp_path / "queue",
        hooks=hooks,
        options=engine_execution.EngineWorkerOptions(emit_output=True),
    )

    assert outcome == "finalized"
    assert calls == [
        ("mark", True),
        ("run", tmp_path / "queue"),
        ("finalize", "result"),
        ("outcome", "result"),
    ]


def test_run_engine_worker_entry_with_spec_builds_adapter(tmp_path: Path) -> None:
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(tmp_path / "allowed")))
    entry = SimpleNamespace(queue_id="q-1")
    calls: list[tuple[str, Any]] = []

    def run_job(
        _cfg: Any,
        _context: Any,
        queue_root: Path,
        _options: engine_execution.EngineWorkerOptions,
    ) -> str:
        calls.append(("run", queue_root))
        return "result"

    def finalize_entry(
        _cfg: Any,
        _context: Any,
        result: str,
        _queue_root: Path,
        options: engine_execution.EngineWorkerOptions,
    ) -> str:
        calls.append(("finalize", options.emit_output))
        return f"{result}:finalized"

    def build_outcome(_context: Any, result: str, finalized: str) -> str:
        calls.append(("outcome", result))
        return finalized

    spec = engine_execution.EngineWorkerExecutionSpec(
        build_context=lambda cfg_obj, entry_obj: SimpleNamespace(
            cfg=cfg_obj,
            entry=entry_obj,
        ),
        mark_running=lambda _cfg, _context, options: calls.append(("mark", options.worker_job_pid)),
        run_job=run_job,
        finalize_entry=finalize_entry,
        build_outcome=build_outcome,
        shutdown_exception_type=RuntimeError,
    )

    outcome = engine_execution.run_engine_worker_entry_with_spec(
        cfg,
        entry,
        queue_root=tmp_path / "queue",
        spec=spec,
        options=engine_execution.EngineWorkerOptions(
            worker_job_pid=101,
            emit_output=True,
        ),
    )

    assert outcome == "result:finalized"
    assert calls == [
        ("mark", 101),
        ("run", tmp_path / "queue"),
        ("finalize", True),
        ("outcome", "result"),
    ]


def test_run_engine_worker_entry_with_spec_options_builds_options(
    tmp_path: Path,
) -> None:
    cfg = object()
    entry = SimpleNamespace(queue_id="q-1")
    running_process = object()
    calls: list[tuple[str, Any]] = []

    def register_running_job(process: object | None) -> None:
        calls.append(("register", process))

    spec = engine_execution.EngineWorkerExecutionSpec(
        build_context=lambda _cfg, current_entry: SimpleNamespace(entry=current_entry),
        mark_running=lambda _cfg, _context, options: calls.append(("mark", options.worker_job_pid)),
        run_job=lambda _cfg, _context, _queue_root, options: (
            (
                options.register_running_job(running_process)
                if options.register_running_job is not None
                else None
            )
            or {
                "should_cancel": None if options.should_cancel is None else options.should_cancel(),
                "shutdown_requested": None
                if options.shutdown_requested is None
                else options.shutdown_requested(),
            }
        ),
        finalize_entry=lambda _cfg, _context, result, _queue_root, options: {
            **result,
            "emit_output": options.emit_output,
        },
        shutdown_exception_type=RuntimeError,
    )

    outcome = engine_execution.run_engine_worker_entry_with_spec_options(
        cfg,
        entry,
        queue_root=tmp_path / "queue",
        spec=spec,
        should_cancel=lambda: True,
        shutdown_requested=lambda: False,
        register_running_job=register_running_job,
        worker_job_pid=101,
        emit_output=True,
    )

    assert outcome == {
        "should_cancel": True,
        "shutdown_requested": False,
        "emit_output": True,
    }
    assert calls == [("mark", 101), ("register", running_process)]


def test_run_engine_worker_entry_with_spec_factory_options_builds_once(
    tmp_path: Path,
) -> None:
    cfg = object()
    entry = SimpleNamespace(queue_id="q-1")
    calls: list[str] = []

    def build_spec() -> engine_execution.EngineWorkerExecutionSpec:
        calls.append("build")
        return engine_execution.EngineWorkerExecutionSpec(
            build_context=lambda _cfg, current_entry: SimpleNamespace(entry=current_entry),
            mark_running=lambda *_args: calls.append("mark"),
            run_job=lambda *_args: "result",
            finalize_entry=lambda *_args: "finalized",
            shutdown_exception_type=RuntimeError,
        )

    outcome = engine_execution.run_engine_worker_entry_with_spec_factory_options(
        cfg,
        entry,
        queue_root=tmp_path / "queue",
        spec_factory=build_spec,
        worker_job_pid=101,
    )

    assert outcome == "finalized"
    assert calls == ["build", "mark"]


def test_raise_if_shutdown_requested_uses_engine_context() -> None:
    class ShutdownRequested(RuntimeError):
        def __init__(self, context: Any) -> None:
            super().__init__("shutdown")
            self.context = context

    context = SimpleNamespace(job_dir="/tmp/job")

    try:
        engine_execution.raise_if_shutdown_requested(
            context,
            engine_execution.EngineWorkerOptions(shutdown_requested=lambda: True),
            shutdown_exception_type=ShutdownRequested,
        )
    except ShutdownRequested as exc:
        assert exc.context is context
    else:
        raise AssertionError("expected shutdown")


def test_raise_if_shutdown_callback_requested_uses_engine_context() -> None:
    class ShutdownRequested(RuntimeError):
        def __init__(self, context: Any) -> None:
            super().__init__("shutdown")
            self.context = context

    context = SimpleNamespace(job_dir="/tmp/job")

    with pytest.raises(ShutdownRequested) as exc_info:
        engine_execution.raise_if_shutdown_callback_requested(
            context,
            lambda: True,
            shutdown_exception_type=ShutdownRequested,
        )

    assert exc_info.value.context is context


def test_queue_cancel_callback_uses_normalized_queue_root_and_entry_id() -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    def get_cancel_requested(
        root: str,
        queue_id: str,
        **kwargs: object,
    ) -> bool:
        calls.append((root, queue_id, kwargs))
        return True

    queue_deps = engine_execution.EngineWorkerQueueDependencies(
        get_cancel_requested=get_cancel_requested,
        mark_completed=lambda *args, **kwargs: None,
        mark_cancelled=lambda *args, **kwargs: None,
        mark_failed=lambda *args, **kwargs: None,
    )

    callback = engine_execution.queue_cancel_callback(
        queue_deps,
        Path("/tmp/queue"),
        SimpleNamespace(queue_id="queue-1", task_id="task-1"),
    )

    assert callback() is True
    assert calls == [
        (
            "/tmp/queue",
            "queue-1",
            {
                "expected_entry": SimpleNamespace(queue_id="queue-1", task_id="task-1"),
                "expected_task_id": "task-1",
            },
        )
    ]


def test_sync_terminal_result_runs_common_terminal_sequence() -> None:
    calls: list[str] = []

    def sync_job_record() -> str:
        calls.append("sync")
        return "organized"

    def mark_queue_terminal(before_update: Any) -> bool:
        before_update()
        calls.append("mark")
        return True

    outcome = engine_execution.sync_terminal_result(
        engine_execution.TerminalSyncActions(
            write_artifacts=lambda: calls.append("write"),
            mark_queue_terminal=mark_queue_terminal,
            sync_job_record=sync_job_record,
            notify_finished=lambda sync_result: calls.append(f"notify:{sync_result}"),
            emit_output=lambda sync_result: calls.append(f"emit:{sync_result}"),
            build_outcome=lambda sync_result: ("outcome", sync_result),
        ),
        emit_output=True,
    )

    assert calls == ["write", "sync", "mark", "notify:organized", "emit:organized"]
    assert outcome == ("outcome", "organized")


def test_sync_terminal_result_leaves_queue_replayable_when_index_sync_fails() -> None:
    calls: list[str] = []

    def fail_sync() -> str:
        calls.append("sync")
        raise OSError("index fsync failed")

    def mark_queue_terminal(before_update: Any) -> bool:
        before_update()
        calls.append("mark")
        return True

    with pytest.raises(OSError, match="index fsync failed"):
        engine_execution.sync_terminal_result(
            engine_execution.TerminalSyncActions(
                write_artifacts=lambda: calls.append("write"),
                sync_job_record=fail_sync,
                mark_queue_terminal=mark_queue_terminal,
                notify_finished=lambda _result: calls.append("notify"),
                build_outcome=lambda result: result,
            )
        )

    assert calls == ["write", "sync"]


def test_sync_terminal_result_does_not_write_for_rejected_generation() -> None:
    calls: list[str] = []

    def handle_rejected() -> str:
        calls.append("fallback")
        return "rejected"

    outcome = engine_execution.sync_terminal_result(
        engine_execution.TerminalSyncActions(
            write_artifacts=lambda: calls.append("write"),
            sync_job_record=lambda: calls.append("sync"),
            mark_queue_terminal=lambda _before_update: None,
            notify_finished=lambda _result: calls.append("notify"),
            build_outcome=lambda _result: "default",
            handle_uncommitted_terminal=handle_rejected,
        )
    )

    assert outcome == "rejected"
    assert calls == ["fallback"]


def test_mark_result_terminal_status_passes_result_fields() -> None:
    calls: list[dict[str, Any]] = []
    expected_entry = SimpleNamespace(queue_id="queue-1", task_id="task-1")

    engine_execution.mark_result_terminal_status(
        "/tmp/queue",
        "queue-1",
        SimpleNamespace(status="completed", reason="ok"),
        metadata_update={"kind": "demo"},
        mark_terminal_status_fn=lambda *args, **kwargs: calls.append(
            {"args": args, "kwargs": kwargs}
        ),
        mark_completed_fn=lambda *args, **kwargs: None,
        mark_cancelled_fn=lambda *args, **kwargs: None,
        mark_failed_fn=lambda *args, **kwargs: None,
        expected_entry=expected_entry,
        expected_task_id="task-1",
    )

    assert calls[0]["args"] == ("/tmp/queue", "queue-1")
    assert calls[0]["kwargs"]["status"] == "completed"
    assert calls[0]["kwargs"]["reason"] == "ok"
    assert calls[0]["kwargs"]["metadata_update"] == {"kind": "demo"}
    assert callable(calls[0]["kwargs"]["mark_completed_fn"])
    assert callable(calls[0]["kwargs"]["mark_cancelled_fn"])
    assert callable(calls[0]["kwargs"]["mark_failed_fn"])
    assert calls[0]["kwargs"]["expected_entry"] is expected_entry
    assert calls[0]["kwargs"]["expected_task_id"] == "task-1"


def test_default_entry_resource_request_uses_common_resource_caps() -> None:
    cfg = SimpleNamespace(
        resources=SimpleNamespace(max_cores_per_task=8, max_memory_gb_per_task=32),
    )
    entry = SimpleNamespace(metadata={"resource_request": {"max_cores": "4"}})

    assert engine_execution.default_engine_resource_caps(cfg) == {
        "max_cores": 8,
        "max_memory_gb": 32,
    }
    assert engine_execution.default_entry_resource_request(cfg, entry) == {"max_cores": 4}


def test_run_cancellable_process_execution_waits_and_clears_running_job() -> None:
    running = SimpleNamespace(process=SimpleNamespace())
    registered: list[Any | None] = []
    wait_calls: list[tuple[Any, dict[str, Any]]] = []

    def wait_for_process(actual_running: Any, **kwargs: Any) -> str:
        wait_calls.append((actual_running, kwargs))
        return "completed"

    outcome = engine_execution.run_cancellable_process_execution(
        engine_execution.CancellableProcessExecution(
            start_job=lambda: running,
            finalize_job=lambda *_args, **_kwargs: "finalized",
            terminate_process=lambda _proc: True,
            build_failure_result=lambda exc: f"failed:{exc}",
            wait_for_cancellable_process=wait_for_process,
            should_cancel=lambda: False,
            sleep=lambda _seconds: None,
            poll_interval_seconds=0.25,
            check_cancel_before_poll=True,
            register_running_job=registered.append,
        )
    )

    assert outcome == "completed"
    assert registered == [running, None]
    assert wait_calls[0][0] is running
    assert wait_calls[0][1]["check_cancel_before_poll"] is True
    assert wait_calls[0][1]["poll_interval_seconds"] == 0.25


def test_run_cancellable_process_execution_builds_failure_result() -> None:
    outcome = engine_execution.run_cancellable_process_execution(
        engine_execution.CancellableProcessExecution(
            start_job=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            finalize_job=lambda *_args, **_kwargs: "finalized",
            terminate_process=lambda _proc: True,
            build_failure_result=lambda exc: f"failed:{exc}",
        )
    )

    assert outcome == "failed:boom"


def test_finalizer_failure_after_group_exit_does_not_terminate_reaped_process() -> None:
    process = SimpleNamespace(poll=lambda: 0)
    running = SimpleNamespace(process=process)
    terminated: list[Any] = []

    def wait_for_process(actual_running: Any, *, finalize_fn: Any, **_kwargs: Any) -> Any:
        return finalize_fn(actual_running)

    def terminate_process(proc: Any) -> bool:
        terminated.append(proc)
        return True

    outcome = engine_execution.run_cancellable_process_execution(
        engine_execution.CancellableProcessExecution(
            start_job=lambda: running,
            finalize_job=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("finalize failed")
            ),
            terminate_process=terminate_process,
            build_failure_result=lambda exc: f"failed:{exc}",
            wait_for_cancellable_process=wait_for_process,
        )
    )

    assert outcome == "failed:finalize failed"
    assert terminated == []


@pytest.mark.parametrize("failure_source", ["cancel", "poll"])
def test_run_cancellable_process_execution_terminates_after_wait_error(
    failure_source: str,
) -> None:
    class Process:
        exited = False

        def poll(self) -> int | None:
            if failure_source == "poll" and not self.exited:
                raise RuntimeError("poll failed")
            return 0 if self.exited else None

    process = Process()
    running = SimpleNamespace(process=process)
    terminated: list[Any] = []
    registered: list[Any | None] = []

    def should_cancel() -> bool:
        if failure_source == "cancel":
            raise RuntimeError("cancel check failed")
        return False

    def terminate(actual_process: Any) -> bool:
        terminated.append(actual_process)
        actual_process.exited = True
        return True

    outcome = engine_execution.run_cancellable_process_execution(
        engine_execution.CancellableProcessExecution(
            start_job=lambda: running,
            finalize_job=lambda *_args, **_kwargs: "finalized",
            terminate_process=terminate,
            build_failure_result=lambda exc: f"failed:{exc}",
            should_cancel=should_cancel,
            check_cancel_before_poll=True,
            register_running_job=registered.append,
        )
    )

    expected_error = "cancel check failed" if failure_source == "cancel" else "poll failed"
    assert outcome == f"failed:{expected_error}"
    assert terminated == [process]
    assert registered == [running, None]


@pytest.mark.parametrize("value", [None, "", "relative/job", {"path": "/tmp/job"}])
def test_entry_metadata_resolved_path_rejects_unsafe_values(value: Any) -> None:
    entry = SimpleNamespace(metadata={"job_dir": value})

    with pytest.raises(ValueError, match="Queue metadata 'job_dir'"):
        engine_execution.entry_metadata_resolved_path(entry, "job_dir")


def test_require_path_within_roots_rejects_symlink_escape(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    outside_root = tmp_path / "outside"
    allowed_root.mkdir()
    outside_root.mkdir()
    escape = allowed_root / "escape"
    escape.symlink_to(outside_root, target_is_directory=True)

    with pytest.raises(ValueError, match="must be under an allowed root"):
        engine_execution.require_path_within_roots(
            escape / "job",
            (allowed_root,),
            label="Queue metadata 'job_dir'",
        )


def test_run_engine_worker_process_job_uses_process_dependency_group() -> None:
    running = SimpleNamespace(process=SimpleNamespace(pid=123))
    registered: list[Any | None] = []
    wait_kwargs: list[dict[str, Any]] = []

    def wait_for_cancellable_process(actual_running: Any, **kwargs: Any) -> str:
        assert actual_running is running
        wait_kwargs.append(kwargs)
        return "done"

    process_deps = engine_execution.EngineWorkerProcessDependencies(
        terminate_process=lambda _proc: True,
        wait_for_cancellable_process=wait_for_cancellable_process,
        sleep=lambda _seconds: None,
        cancel_check_interval_seconds=0.75,
    )

    result = engine_execution.run_engine_worker_process_job(
        SimpleNamespace(job_dir="/tmp/job"),
        options=engine_execution.EngineWorkerOptions(
            should_cancel=lambda: False,
            register_running_job=registered.append,
        ),
        process_deps=process_deps,
        shutdown_exception_type=RuntimeError,
        start_job=lambda: running,
        finalize_job=lambda *_args, **_kwargs: "finalized",
        build_failure_result=lambda exc: f"failed:{exc}",
        check_cancel_before_poll=True,
    )

    assert result == "done"
    assert registered == [running, None]
    assert wait_kwargs[0]["poll_interval_seconds"] == 0.75
    assert wait_kwargs[0]["check_cancel_before_poll"] is True


def test_build_engine_worker_dependency_groups_preserve_extra_fields() -> None:
    timing = engine_execution.build_engine_worker_timing_dependencies(
        SimpleNamespace,
        now_utc_iso=lambda: "now",
    )
    queue = engine_execution.build_engine_worker_queue_dependencies(
        SimpleNamespace,
        get_cancel_requested=lambda _root, _queue_id: True,
        mark_completed=lambda *_args, **_kwargs: None,
        mark_cancelled=lambda *_args, **_kwargs: None,
        mark_failed=lambda *_args, **_kwargs: None,
    )
    process = engine_execution.build_engine_worker_process_dependencies(
        SimpleNamespace,
        terminate_process=lambda _proc: True,
        wait_for_cancellable_process=lambda *_args, **_kwargs: "done",
        sleep=lambda _seconds: None,
        cancel_check_interval_seconds=0.5,
        engine="xtb",
    )

    assert timing.now_utc_iso() == "now"
    assert queue.get_cancel_requested("/tmp/queue", "queue-1") is True
    assert process.cancel_check_interval_seconds == 0.5
    assert process.engine == "xtb"


def test_run_cancellable_process_execution_can_reraise_policy_exceptions() -> None:
    class ShutdownRequested(RuntimeError):
        pass

    def wait_for_process(_running: Any, **_kwargs: Any) -> str:
        raise ShutdownRequested("shutdown")

    process = SimpleNamespace(exited=False)
    process.poll = lambda: 0 if process.exited else None

    def terminate(_process: Any) -> bool:
        process.exited = True
        return True

    try:
        engine_execution.run_cancellable_process_execution(
            engine_execution.CancellableProcessExecution(
                start_job=lambda: SimpleNamespace(process=process),
                finalize_job=lambda *_args, **_kwargs: "finalized",
                terminate_process=terminate,
                build_failure_result=lambda exc: f"failed:{exc}",
                wait_for_cancellable_process=wait_for_process,
                should_reraise_exception=lambda exc: isinstance(exc, ShutdownRequested),
            )
        )
    except ShutdownRequested as exc:
        assert str(exc) == "shutdown"
    else:
        raise AssertionError("expected shutdown")


@pytest.mark.parametrize(
    ("termination_mode", "error_match"),
    [
        ("false", "did not confirm"),
        ("raises", "raised before termination"),
        ("still_running", "reported success while the process is still running"),
    ],
)
def test_cleanup_failure_retries_and_never_builds_terminal_result(
    termination_mode: str,
    error_match: str,
) -> None:
    class Process:
        exited = False

        def poll(self) -> int | None:
            return 0 if self.exited else None

    process = Process()
    terminate_calls: list[Process] = []
    finalize_calls: list[Any] = []
    failure_calls: list[Exception] = []
    sleeps: list[float] = []
    registered: list[Any | None] = []

    def terminate(actual_process: Process) -> bool:
        terminate_calls.append(actual_process)
        if len(terminate_calls) == 3:
            actual_process.exited = True
        if termination_mode == "raises":
            raise RuntimeError("termination callback failed")
        return termination_mode == "still_running"

    def sleep(seconds: float) -> None:
        assert registered == [running]
        sleeps.append(seconds)

    running = SimpleNamespace(process=process)

    with pytest.raises(engine_execution.ProcessCleanupError, match=error_match):
        engine_execution.run_cancellable_process_execution(
            engine_execution.CancellableProcessExecution(
                start_job=lambda: running,
                finalize_job=lambda *_args, **_kwargs: finalize_calls.append(True),
                terminate_process=terminate,
                build_failure_result=lambda exc: failure_calls.append(exc),
                should_cancel=lambda: True,
                sleep=sleep,
                check_cancel_before_poll=True,
                register_running_job=registered.append,
                cleanup_retry_attempts=2,
                cleanup_poll_interval_seconds=0.25,
            )
        )

    assert terminate_calls == [process, process, process]
    assert sleeps == [0.25, 0.25]
    assert process.exited is True
    assert finalize_calls == []
    assert failure_calls == []
    assert registered == [running, None]


def test_run_cancellable_engine_process_builds_common_execution_actions() -> None:
    running = SimpleNamespace(process=SimpleNamespace())
    registered: list[Any | None] = []
    wait_kwargs: list[dict[str, Any]] = []

    def wait_for_process(actual_running: Any, **kwargs: Any) -> str:
        assert actual_running is running
        wait_kwargs.append(kwargs)
        return "done"

    outcome = engine_execution.run_cancellable_engine_process(
        start_job=lambda: running,
        finalize_job=lambda *_args, **_kwargs: "finalized",
        terminate_process=lambda _proc: True,
        build_failure_result=lambda exc: f"failed:{exc}",
        wait_for_cancellable_process=wait_for_process,
        should_cancel=lambda: False,
        sleep=lambda _seconds: None,
        poll_interval_seconds=0.5,
        check_cancel_before_poll=True,
        register_running_job=registered.append,
    )

    assert outcome == "done"
    assert registered == [running, None]
    assert wait_kwargs[0]["poll_interval_seconds"] == 0.5
    assert wait_kwargs[0]["check_cancel_before_poll"] is True
