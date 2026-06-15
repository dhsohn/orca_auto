from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orca_auto.core.engines.definitions import EngineDefinition, EngineQueueFunctions
from orca_auto.core.queue.engine_runtime import EngineQueueRuntime
from orca_auto.core.queue.internal_engine import InternalEngineQueueModule, InternalEngineSpec
from orca_auto.core.queue.internal_engine_worker_deps import (
    InternalEngineQueueWorkerDeps,
    InternalEngineQueueWorkerFacadeBindings,
    InternalEngineQueueWorkerFacadeCallbacks,
    build_internal_engine_queue_worker_deps,
    build_late_bound_internal_engine_queue_worker_deps,
)
from orca_auto.core.queue.worker_execution_dependencies import (
    WorkerAdmissionDependencies,
    WorkerConfigDependencies,
    WorkerProcessDependencyCallbacks,
    build_worker_process_default_factories_from_callbacks,
    build_worker_process_dependency_callbacks,
    build_worker_process_dependency_groups,
    run_worker_child_entrypoint,
    run_worker_child_entrypoint_with_dependencies,
    worker_process_dependency_callback_kwargs,
    worker_process_dependency_callbacks_from_attrs,
)


def _runtime(
    tmp_path: Path,
    *,
    entries: dict[Path, list[Any]] | None = None,
    dequeued: dict[Path, Any | None] | None = None,
) -> EngineQueueRuntime:
    return EngineQueueRuntime(
        load_config=lambda value: value,
        runtime_roots_for_cfg=lambda _cfg: (tmp_path / "a", tmp_path / "b"),
        list_queue=lambda root: dict(entries or {}).get(Path(root), []),
        dequeue_next=lambda root: dict(dequeued or {}).get(root),
        worker_pid_file_name="engine_worker.pid",
    )


def test_engine_queue_runtime_combines_roots_entries_and_next_entry(
    tmp_path: Path,
) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    entry_a = SimpleNamespace(queue_id="a", status=SimpleNamespace(value="pending"))
    entry_b = SimpleNamespace(
        queue_id="b",
        status=SimpleNamespace(value="pending"),
        priority=1,
        enqueued_at="2026-01-01T00:00:00Z",
        cancel_requested=False,
    )
    runtime = _runtime(
        tmp_path,
        entries={root_a: [entry_a], root_b: [entry_b]},
        dequeued={root_b: entry_b},
    )

    assert runtime.queue_roots(SimpleNamespace()) == (root_a, root_b)
    assert runtime.queue_entries_with_roots(SimpleNamespace()) == [
        (root_a, entry_a),
        (root_b, entry_b),
    ]
    assert runtime.dequeue_next_entry(SimpleNamespace()) == (root_b, entry_b)


def test_engine_queue_runtime_common_accessors(tmp_path: Path) -> None:
    entry = SimpleNamespace(queue_id="queue-1")
    runtime = _runtime(tmp_path, entries={tmp_path / "a": [entry]})
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root="/tmp/allowed",
            admission_root="/tmp/admission",
            admission_limit=1,
            max_concurrent=1,
        )
    )

    assert runtime.queue_entry_by_id(tmp_path / "a", "queue-1") is entry
    assert runtime.admission_root(cfg) == "/tmp/admission"

    (tmp_path / "engine_worker.pid").write_text("123\n", encoding="utf-8")
    assert runtime.read_worker_pid(tmp_path) is None


def test_engine_queue_runtime_builds_child_worker_deps(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    entry = SimpleNamespace(queue_id="queue-1", status=SimpleNamespace(value="pending"))
    runtime = _runtime(
        tmp_path,
        entries={tmp_path / "a": [entry]},
        dequeued={tmp_path / "a": entry},
    )
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root="/tmp/allowed",
            admission_root="/tmp/admission",
            admission_limit=1,
            max_concurrent=1,
        )
    )
    released: list[tuple[str, str]] = []
    started: list[dict[str, Any]] = []

    deps = runtime.child_worker_deps(
        poll_interval_seconds=5,
        time_module=SimpleNamespace(sleep=lambda _seconds: None),
        release_slot_fn=lambda root, token: released.append((str(root), token)),
        start_background_job_process_fn=lambda **kwargs: started.append(kwargs),
        try_reserve_admission_slot_fn=lambda _cfg: "slot-1",
    )

    assert deps.admission_root(cfg) == "/tmp/admission"
    assert deps.dequeue_next_entry(cfg) == (tmp_path / "a", entry)
    status, reserved = deps.reserve_dequeued_entry(
        cfg,
        admission_root=deps.admission_root(cfg),
        reserve_slot_fn=deps.try_reserve_admission_slot,
        dequeue_next_fn=deps.dequeue_next_entry,
        release_slot_fn=deps.release_slot,
    )

    assert status == "processed"
    assert reserved is not None
    assert reserved.queue_root == tmp_path / "a"
    assert reserved.entry is entry
    assert reserved.admission_token == "slot-1"
    assert released == []

    deps.start_background_job_process(
        config_path="/tmp/config.yaml",
        queue_root=reserved.queue_root,
        entry=reserved.entry,
        admission_root=deps.admission_root(cfg),
        admission_token=reserved.admission_token,
    )

    assert started == [
        {
            "config_path": "/tmp/config.yaml",
            "queue_root": tmp_path / "a",
            "entry": entry,
            "admission_root": "/tmp/admission",
            "admission_token": "slot-1",
        }
    ]


def test_late_bound_internal_engine_queue_worker_deps_use_current_callbacks(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def record(name: str, result: Any = None) -> Any:
        def _call(*args: Any, **kwargs: Any) -> Any:
            calls.append((name, args, kwargs))
            return result

        return _call

    release_slot_fn: Any = record("initial_release", "initial")
    bindings = InternalEngineQueueWorkerFacadeBindings(
        release_slot=lambda: release_slot_fn,
        reserve_slot=lambda: record("reserve", "slot-1"),
        start_background_process=lambda: record("start_background_process", "process"),
        build_worker_child_command=lambda: record(
            "build_worker_child_command",
            ["worker"],
        ),
        activate_reserved_slot=lambda: record("activate_reserved_slot", object()),
        terminate_process=lambda: record("terminate_process"),
        mark_failed=lambda: record("mark_failed"),
        handle_worker_start_error=lambda: record("handle_worker_start_error"),
        finalize_completed_job=lambda: record("finalize_completed_job"),
        finalize_child_exit=lambda: record("finalize_child_exit"),
        reconcile_worker_state=lambda: record("reconcile_worker_state"),
        list_queue=lambda: record("list_queue", []),
        list_slots=lambda: record("list_slots", []),
        reconcile_stale_slots=lambda: record("reconcile_stale_slots"),
        mark_cancelled=lambda: record("mark_cancelled"),
        requeue_running_entry=lambda: record("requeue_running_entry"),
        default_config_path=lambda: record("default_config_path", "/tmp/default.yaml"),
        find_queue_entry=lambda: record("find_queue_entry", "entry"),
    )

    deps = build_late_bound_internal_engine_queue_worker_deps(
        bindings,
        time_module=SimpleNamespace(sleep=lambda _seconds: None),
    )
    release_slot_fn = record("updated_release", "released")

    assert deps.release_slot(tmp_path, "slot-1") == "released"
    assert deps.default_config_path() == "/tmp/default.yaml"
    assert deps.find_queue_entry is not None
    assert deps.find_queue_entry(tmp_path, "queue-1") == "entry"
    assert calls == [
        ("updated_release", (tmp_path, "slot-1"), {}),
        ("default_config_path", (), {}),
        ("find_queue_entry", (tmp_path, "queue-1"), {}),
    ]


def test_internal_engine_queue_worker_deps_builder_maps_callbacks(tmp_path: Path) -> None:
    calls: list[str] = []

    def record(name: str, result: Any = None) -> Any:
        def _call(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(name)
            return result

        return _call

    time_module = SimpleNamespace(sleep=lambda _seconds: None)
    deps = build_internal_engine_queue_worker_deps(
        InternalEngineQueueWorkerFacadeCallbacks(
            release_slot=record("release", "released"),
            reserve_slot=record("reserve", "reserved"),
            start_background_process=record("start_process", "process"),
            build_worker_child_command=record("build_command", ["worker"]),
            config_path_for_worker=record("config_path", "/tmp/config.yaml"),
            activate_reserved_slot=record("activate", object()),
            terminate_process=record("terminate"),
            mark_failed=record("mark_failed"),
            handle_worker_start_error=record("start_error"),
            finalize_completed_job=record("completed"),
            finalize_child_exit=record("child_exit"),
            reconcile_worker_state=record("reconcile_worker"),
            list_queue=record("list_queue", []),
            list_slots=record("list_slots", []),
            reconcile_stale_slots=record("stale_slots"),
            mark_cancelled=record("cancelled"),
            requeue_running_entry=record("requeue"),
            start_background_job_process=record("start_job", "job-process"),
            find_queue_entry=record("find_entry", "entry"),
        ),
        time_module=time_module,
    )

    assert deps.time_module is time_module
    assert deps.release_slot(tmp_path, "slot-1") == "released"
    assert deps.default_config_path() == ""
    assert deps.reconcile_orphaned_child_queue_entries("root") is None
    assert deps.start_background_job_process_fn is not None
    assert (
        deps.start_background_job_process_fn(
            config_path="/tmp/config.yaml",
            queue_root=tmp_path,
            entry=SimpleNamespace(queue_id="queue-1"),
            admission_root=tmp_path,
            admission_token="slot-1",
        )
        == "job-process"
    )
    assert deps.find_queue_entry is not None
    assert deps.find_queue_entry(tmp_path, "queue-1") == "entry"
    assert calls == ["release", "start_job", "find_entry"]


def test_worker_process_dependency_callbacks_from_attrs_maps_common_callbacks() -> None:
    def record(name: str) -> Any:
        def _call(*_args: Any, **_kwargs: Any) -> str:
            return name

        return _call

    source = SimpleNamespace(
        terminate_process=record("terminate"),
        wait_for_cancellable_process=record("wait"),
        sleep=record("sleep"),
        now_utc_iso=lambda: "2026-01-01T00:00:00+00:00",
        get_cancel_requested=record("cancel"),
        mark_completed=record("completed"),
        mark_cancelled=record("cancelled"),
        mark_failed=record("failed"),
        run_demo_job=record("run"),
    )

    callbacks = worker_process_dependency_callbacks_from_attrs(
        source,
        engine_runner_dependency_names=("run_demo_job",),
    )
    kwargs = worker_process_dependency_callback_kwargs(callbacks)
    kwargs_with_runner = worker_process_dependency_callback_kwargs(
        callbacks,
        include_engine_runner_dependencies=True,
    )
    rebuilt = build_worker_process_dependency_callbacks(
        **kwargs,
        engine_runner_dependencies=callbacks.engine_runner_dependencies,
    )

    assert callbacks.terminate_process is source.terminate_process
    assert callbacks.engine_runner_dependencies["run_demo_job"] is source.run_demo_job
    assert kwargs["mark_failed"] is source.mark_failed
    assert kwargs_with_runner["run_demo_job"] is source.run_demo_job
    assert rebuilt.sleep() == "sleep"
    assert rebuilt.engine_runner_dependencies["run_demo_job"]() == "run"


def test_worker_process_dependency_groups_maps_callback_groups() -> None:
    calls: list[str] = []

    def record(name: str) -> Any:
        def _call(*_args: Any, **_kwargs: Any) -> None:
            calls.append(name)

        return _call

    callbacks = WorkerProcessDependencyCallbacks(
        terminate_process=record("terminate"),
        wait_for_cancellable_process=record("wait"),
        sleep=record("sleep"),
        now_utc_iso=lambda: "2026-01-01T00:00:00+00:00",
        get_cancel_requested=record("cancel"),
        mark_completed=record("completed"),
        mark_cancelled=record("cancelled"),
        mark_failed=record("failed"),
        engine_runner_dependencies={"run_demo_job": record("run")},
    )

    groups = build_worker_process_dependency_groups(
        callbacks,
        timing_dependencies_type=SimpleNamespace,
        queue_dependencies_type=SimpleNamespace,
        runner_dependencies_type=SimpleNamespace,
        cancel_check_interval_seconds=6,
    )

    assert groups["timing"].now_utc_iso() == "2026-01-01T00:00:00+00:00"
    groups["queue"].mark_completed("root", "queue-1")
    assert groups["runner"].cancel_check_interval_seconds == 6
    groups["runner"].run_demo_job()
    assert calls == ["completed", "run"]


def test_worker_process_default_factories_from_callbacks_maps_common_groups() -> None:
    calls: list[str] = []

    def record(name: str) -> Any:
        def _call(*_args: Any, **_kwargs: Any) -> None:
            calls.append(name)

        return _call

    callbacks = WorkerProcessDependencyCallbacks(
        terminate_process=record("terminate"),
        wait_for_cancellable_process=record("wait"),
        sleep=record("sleep"),
        now_utc_iso=lambda: "2026-01-01T00:00:00+00:00",
        get_cancel_requested=record("cancel"),
        mark_completed=record("completed"),
        mark_cancelled=record("cancelled"),
        mark_failed=record("failed"),
        engine_runner_dependencies={"run_demo_job": record("run")},
    )

    factories = build_worker_process_default_factories_from_callbacks(
        callbacks,
        config_factory=lambda: "config",
        admission_factory=lambda: "admission",
        timing_dependencies_type=SimpleNamespace,
        queue_dependencies_type=SimpleNamespace,
        runner_dependencies_type=SimpleNamespace,
        cancel_check_interval_seconds=8,
    )

    assert factories["config"]() == "config"
    assert factories["admission"]() == "admission"
    assert factories["timing"]().now_utc_iso() == "2026-01-01T00:00:00+00:00"
    factories["queue"]().mark_failed("root", "queue-1")
    runner = factories["runner"]()
    assert runner.cancel_check_interval_seconds == 8
    assert runner.run_demo_job is callbacks.engine_runner_dependencies["run_demo_job"]
    assert calls == ["failed"]


def test_run_worker_child_entrypoint_wires_common_child_kwargs(tmp_path: Path) -> None:
    calls: dict[str, Any] = {}
    installer_token = object()

    class WorkerChild:
        @staticmethod
        def shutdown_signal_handler_installer(install_fn: Any) -> Any:
            calls["install_fn"] = install_fn
            return installer_token

        @staticmethod
        def run_worker_child_job(**kwargs: Any) -> int:
            calls["kwargs"] = kwargs
            return 9

    process_kwargs = {"worker_config_path": "/tmp/config.yaml"}

    rc = run_worker_child_entrypoint(
        WorkerChild(),
        config_path="/tmp/config.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
        load_config_fn=lambda path: path,
        find_queue_entry_fn=lambda _root, _queue_id: None,
        admission_root_fn=lambda _cfg: "/tmp/admission",
        release_slot_fn=lambda *_args: None,
        install_shutdown_signal_handlers_fn=lambda *_args: None,
        process_dequeued_entry_fn=lambda *_args, **_kwargs: None,
        dependencies_fn=lambda: object(),
        requeue_running_entry_fn=lambda *_args: None,
        mark_recovery_pending_context_fn=lambda *_args, **_kwargs: None,
        process_dequeued_entry_kwargs=process_kwargs,
    )

    assert rc == 9
    assert calls["kwargs"]["install_signal_handlers_fn"] is installer_token
    assert calls["kwargs"]["queue_id"] == "queue-1"
    assert calls["kwargs"]["process_dequeued_entry_kwargs"] is process_kwargs


def test_run_worker_child_entrypoint_with_dependencies_wires_config_and_admission(
    tmp_path: Path,
) -> None:
    calls: dict[str, Any] = {}
    cfg = SimpleNamespace(name="cfg")
    entry = SimpleNamespace(queue_id="queue-1")
    released: list[tuple[str, str]] = []
    installer_token = object()
    dependencies = SimpleNamespace(
        config=WorkerConfigDependencies(
            load_config=lambda path: cfg if path == "/tmp/config.yaml" else None,
            queue_entry_by_id=lambda _root, _queue_id: entry,
        ),
        admission=WorkerAdmissionDependencies(
            activate_reserved_slot=lambda *_args, **_kwargs: object(),
            release_slot=lambda root, token: released.append((str(root), token)),
        ),
    )

    class WorkerChild:
        @staticmethod
        def shutdown_signal_handler_installer(install_fn: Any) -> Any:
            calls["install_fn"] = install_fn
            return installer_token

        @staticmethod
        def run_worker_child_job(**kwargs: Any) -> int:
            calls["kwargs"] = kwargs
            assert kwargs["load_config_fn"]("/tmp/config.yaml") is cfg
            assert kwargs["find_queue_entry_fn"](tmp_path / "queue", "queue-1") is entry
            kwargs["release_slot_fn"]("/tmp/admission", "slot-1")
            assert kwargs["dependencies_fn"]() is dependencies
            return 7

    rc = run_worker_child_entrypoint_with_dependencies(
        WorkerChild(),
        config_path="/tmp/config.yaml",
        queue_root=tmp_path / "queue",
        queue_id="queue-1",
        admission_token="slot-1",
        dependencies=dependencies,
        admission_root_fn=lambda _cfg: "/tmp/admission",
        install_shutdown_signal_handlers_fn=lambda *_args: None,
        process_dequeued_entry_fn=lambda *_args, **_kwargs: None,
        requeue_running_entry_fn=lambda *_args: None,
        mark_recovery_pending_context_fn=lambda *_args, **_kwargs: None,
    )

    assert rc == 7
    assert calls["kwargs"]["install_signal_handlers_fn"] is installer_token
    assert released == [("/tmp/admission", "slot-1")]


def test_engine_queue_runtime_reserves_admission_slot(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root="/tmp/allowed",
            admission_root="/tmp/admission",
            admission_limit=2,
            max_concurrent=4,
        )
    )
    calls: list[dict[str, Any]] = []

    def reserve_slot(root: str, limit: int, **kwargs: Any) -> str:
        calls.append({"root": root, "limit": limit, "kwargs": kwargs})
        return "slot-1"

    assert (
        runtime.reserve_admission_slot(
            cfg,
            engine="xtb",
            reserve_slot_fn=reserve_slot,
        )
        == "slot-1"
    )
    assert calls == [
        {
            "root": "/tmp/admission",
            "limit": 2,
            "kwargs": {
                "source": "orca_auto.flow.engines.xtb.queue_worker",
                "app_name": "orca_auto_xtb",
            },
        }
    ]


def test_engine_queue_runtime_starts_child_process_with_optional_admission_root(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    entry = SimpleNamespace(queue_id="queue-1")
    commands: list[list[str]] = []

    def build_child_command(**kwargs: Any) -> list[str]:
        return [f"{key}={value}" for key, value in sorted(kwargs.items())]

    def start_background_process(command: list[str]) -> str:
        commands.append(command)
        return "proc"

    result = runtime.start_child_process(
        config_path="/tmp/config.yaml",
        queue_root=tmp_path / "queue",
        entry=entry,
        admission_root="/tmp/admission",
        admission_token="slot-1",
        start_background_process_fn=start_background_process,
        build_worker_child_command_fn=build_child_command,
        include_admission_root=False,
    )

    assert result == "proc"
    assert commands == [
        [
            "admission_token=slot-1",
            "config_path=/tmp/config.yaml",
            "queue_id=queue-1",
            f"queue_root={tmp_path / 'queue'}",
        ]
    ]


def test_engine_queue_runtime_builds_common_child_worker_hooks(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    entry = SimpleNamespace(
        queue_id="queue-1",
        metadata={"job_dir": str(tmp_path / "job-1")},
    )
    events: list[tuple[str, Any]] = []

    class Worker:
        admission_root = "/tmp/admission"

        def _mark_entry_failed_and_release(
            self,
            queue_root: Path,
            entry_arg: Any,
            admission_token: str,
            **kwargs: Any,
        ) -> None:
            events.append(
                (
                    "failed_release",
                    {
                        "queue_root": queue_root,
                        "queue_id": entry_arg.queue_id,
                        "admission_token": admission_token,
                        "kwargs": kwargs,
                    },
                )
            )

    class Process:
        pid = 2468

        def __init__(self) -> None:
            self.terminated = False

        def poll(self) -> int | None:
            return 0 if self.terminated else None

        def terminate(self) -> None:
            self.terminated = True

    def handle_worker_start_error(
        worker: Any,
        queue_root: Path,
        entry_arg: Any,
        admission_token: str,
        exc: OSError,
    ) -> None:
        events.append(("start_error", (queue_root, entry_arg.queue_id, admission_token, str(exc))))

    def finalize_completed_job(worker: Any, queue_id: str, job: Any, rc: int) -> None:
        events.append(("completed", (queue_id, job.entry.queue_id, rc)))

    def finalize_child_exit(worker: Any, job: Any, *, rc: int) -> None:
        events.append(("child_exit", (job.entry.queue_id, rc)))

    def reconcile_worker_state(worker: Any) -> None:
        events.append(("reconcile", worker.admission_root))

    def activate_reserved_slot(root: str, token: str, **kwargs: Any) -> object:
        events.append(("activated", {"root": root, "token": token, "kwargs": kwargs}))
        return object()

    hooks = runtime.child_worker_hooks(
        engine="xtb",
        handle_worker_start_error_fn=handle_worker_start_error,
        finalize_completed_job_fn=finalize_completed_job,
        finalize_child_exit_fn=finalize_child_exit,
        reconcile_worker_state_fn=reconcile_worker_state,
        activate_reserved_slot_fn=activate_reserved_slot,
        terminate_process_fn=lambda process: events.append(("terminate", process.pid)),
        mark_failed_fn=lambda *args, **kwargs: events.append(("mark_failed", (args, kwargs))),
        shutdown_grace_seconds=0,
        sleep_fn=lambda seconds: events.append(("sleep", seconds)),
    )

    worker = Worker()
    process = Process()
    assert hooks.on_worker_process_started(
        worker,
        tmp_path,
        entry,
        process,
        "slot-1",
    )
    hooks.finalize_completed_job(worker, "queue-1", SimpleNamespace(entry=entry), 0)
    hooks.reconcile_worker_state(worker)
    hooks.handle_worker_start_error(worker, tmp_path, entry, "slot-2", OSError("boom"))
    hooks.shutdown_running_job(
        worker,
        "queue-1",
        SimpleNamespace(queue_root=tmp_path, entry=entry, process=Process()),
    )

    assert events == [
        (
            "activated",
            {
                "root": "/tmp/admission",
                "token": "slot-1",
                "kwargs": {
                    "owner_pid": 2468,
                    "source": "orca_auto.flow.engines.xtb.queue_worker.child",
                    "queue_id": "queue-1",
                    "work_dir": str(tmp_path / "job-1"),
                },
            },
        ),
        ("completed", ("queue-1", "queue-1", 0)),
        ("reconcile", "/tmp/admission"),
        ("start_error", (tmp_path, "queue-1", "slot-2", "boom")),
        ("child_exit", ("queue-1", 0)),
    ]


def test_engine_queue_runtime_child_worker_hooks_accept_engine_overrides(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    events: list[tuple[str, Any]] = []
    worker = SimpleNamespace(name="worker")
    entry = SimpleNamespace(queue_id="queue-override")
    process = SimpleNamespace(pid=8642)
    job = SimpleNamespace(name="job")

    def record_started(
        worker_arg: Any,
        queue_root: Path,
        entry_arg: Any,
        process_arg: Any,
        token: str,
    ) -> bool:
        events.append(
            (
                "started",
                (worker_arg.name, queue_root, entry_arg.queue_id, process_arg.pid, token),
            )
        )
        return True

    hooks = runtime.child_worker_hooks(
        engine="orca",
        handle_worker_start_error_fn=lambda *args: events.append(("start_error", args)),
        finalize_completed_job_fn=lambda *args: events.append(("completed", args)),
        finalize_child_exit_fn=lambda *args, **kwargs: events.append(
            ("child_exit", (args, kwargs))
        ),
        reconcile_worker_state_fn=lambda worker_arg: events.append(("reconcile", worker_arg.name)),
        activate_reserved_slot_fn=lambda *args, **kwargs: events.append(
            ("activate", (args, kwargs))
        ),
        terminate_process_fn=lambda process_arg: events.append(("terminate", process_arg.pid)),
        mark_failed_fn=lambda *args, **kwargs: events.append(("failed", (args, kwargs))),
        shutdown_grace_seconds=10,
        sleep_fn=lambda seconds: events.append(("sleep", seconds)),
        on_worker_process_started_fn=record_started,
        shutdown_running_job_fn=lambda worker_arg, queue_id, job_arg: events.append(
            ("shutdown", (worker_arg.name, queue_id, job_arg.name))
        ),
        before_shutdown_all_fn=lambda worker_arg, count: events.append(
            ("before_shutdown", (worker_arg.name, count))
        ),
    )

    assert hooks.on_worker_process_started(worker, tmp_path, entry, process, "slot-1")
    hooks.shutdown_running_job(worker, "queue-override", job)
    before_shutdown_all = hooks.before_shutdown_all
    assert before_shutdown_all is not None
    before_shutdown_all(worker, 3)
    hooks.reconcile_worker_state(worker)

    assert events == [
        ("started", ("worker", tmp_path, "queue-override", 8642, "slot-1")),
        ("shutdown", ("worker", "queue-override", "job")),
        ("before_shutdown", ("worker", 3)),
        ("reconcile", "worker"),
    ]


def test_engine_queue_runtime_runs_pidfile_worker_command(tmp_path: Path) -> None:
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(tmp_path),
            admission_root="",
            admission_limit=None,
            max_concurrent=4,
        )
    )
    runtime = EngineQueueRuntime(
        load_config=lambda config: cfg if config == "/tmp/config.yaml" else None,
        runtime_roots_for_cfg=lambda _cfg: (),
        list_queue=lambda _root: [],
        dequeue_next=lambda _root: None,
        worker_pid_file_name="engine_worker.pid",
    )
    calls: list[dict[str, Any]] = []

    class Worker:
        def __init__(self, cfg_arg: Any, config_path: str, **kwargs: Any) -> None:
            calls.append(
                {
                    "cfg": cfg_arg,
                    "config_path": config_path,
                    "kwargs": kwargs,
                }
            )

        def run(self) -> int:
            return 7

    result = runtime.run_pidfile_worker_command(
        SimpleNamespace(config="/tmp/config.yaml"),
        config_path_fn=lambda args: str(args.config),
        worker_factory=Worker,
    )

    assert result == 7
    assert calls == [
        {
            "cfg": cfg,
            "config_path": "/tmp/config.yaml",
            "kwargs": {"max_concurrent": 4},
        }
    ]


def test_engine_queue_runtime_pidfile_command_reports_existing_worker(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(tmp_path)))
    runtime = EngineQueueRuntime(
        load_config=lambda _config: cfg,
        runtime_roots_for_cfg=lambda _cfg: (),
        list_queue=lambda _root: [],
        dequeue_next=lambda _root: None,
        worker_pid_file_name="engine_worker.pid",
    )
    reports: list[int] = []

    result = runtime.run_pidfile_worker_command(
        SimpleNamespace(config="/tmp/config.yaml"),
        config_path_fn=lambda args: str(args.config),
        read_worker_pid_fn=lambda root: 12345 if root == tmp_path else None,
        existing_pid_report_fn=lambda pid: reports.append(pid),
        worker_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker should not start")
        ),
    )

    assert result == 1
    assert reports == [12345]


def test_internal_engine_queue_module_preserves_worker_facade_contract(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Any]] = []

    def record(name: str, result: Any = None) -> Any:
        def _call(*args: Any, **kwargs: Any) -> Any:
            calls.append((name, {"args": args, "kwargs": kwargs}))
            return result

        return _call

    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(tmp_path),
            admission_root=str(tmp_path / "admission"),
            admission_limit=1,
            max_concurrent=1,
        )
    )
    entry = SimpleNamespace(queue_id="queue-1", status=SimpleNamespace(value="pending"))
    deps = build_internal_engine_queue_worker_deps(
        InternalEngineQueueWorkerFacadeCallbacks(
            release_slot=record("release_slot"),
            reserve_slot=record("reserve_slot", "slot-1"),
            start_background_process=record("start_background_process", "process"),
            build_worker_child_command=record("build_worker_child_command", ["worker"]),
            config_path_for_worker=record("config_path_for_worker", "/tmp/config.yaml"),
            default_config_path=record("default_config_path", "/tmp/default.yaml"),
            activate_reserved_slot=record("activate_reserved_slot", object()),
            terminate_process=record("terminate_process"),
            mark_failed=record("mark_failed"),
            handle_worker_start_error=record("handle_worker_start_error"),
            finalize_completed_job=record("finalize_completed_job"),
            finalize_child_exit=record("finalize_child_exit"),
            reconcile_worker_state=record("reconcile_worker_state"),
            list_queue=record("list_queue", []),
            list_slots=record("list_slots", []),
            reconcile_stale_slots=record("reconcile_stale_slots"),
            reconcile_orphaned_child_queue_entries=record("reconcile_orphaned"),
            mark_cancelled=record("mark_cancelled"),
            requeue_running_entry=record("requeue_running_entry"),
            mark_recovery_pending=record("mark_recovery_pending"),
            try_reserve_admission_slot=record("try_reserve_admission_slot", "slot-override"),
            start_background_job_process=record("start_background_job_process", "started"),
            load_config=record("load_config", cfg),
            read_worker_pid=record("read_worker_pid", None),
            worker_class=record("QueueWorker", SimpleNamespace(run=lambda: 0)),
        ),
        time_module=SimpleNamespace(sleep=lambda _seconds: None),
    )
    spec = InternalEngineSpec(
        engine="xtb",
        worker_job_module="orca_auto.flow.engines.xtb.execution",
        worker_pid_file_name="engine_worker.pid",
    )
    module = InternalEngineQueueModule.create(
        spec=spec,
        load_config=lambda _config: cfg,
        runtime_roots_for_cfg=lambda _cfg: (tmp_path,),
        list_queue=lambda _root: [entry],
        dequeue_next=lambda _root: entry,
        poll_interval_seconds=5,
        shutdown_grace_seconds=1.0,
        deps=deps,
    )

    assert module.queue_worker_deps().dequeue_next_entry(cfg) == (tmp_path, entry)
    assert module.queue_worker_hooks() is not None
    assert module.try_reserve_admission_slot(cfg) == "slot-1"
    assert (
        module.start_background_job_process(
            config_path="/tmp/config.yaml",
            queue_root=tmp_path,
            entry=entry,
            admission_root=tmp_path / "admission",
            admission_token="slot-1",
        )
        == "process"
    )
    assert module.config_path_for_worker(SimpleNamespace(config="/tmp/config.yaml")) == (
        "/tmp/config.yaml"
    )

    override_cfg = SimpleNamespace(
        runtime=SimpleNamespace(allowed_root=str(tmp_path), max_concurrent=3)
    )
    worker_calls: list[Any] = []

    class OverrideWorker:
        def __init__(self, cfg_obj: Any, config_path: str, **kwargs: Any) -> None:
            worker_calls.append((cfg_obj, config_path, kwargs))

        def run(self) -> int:
            worker_calls.append("run")
            return 5

    assert (
        module.run_pidfile_worker_command(
            SimpleNamespace(config="/tmp/override.yaml"),
            config_path_fn=lambda args: str(args.config),
            load_config_fn=lambda _config_path: override_cfg,
            read_worker_pid_fn=lambda _allowed_root: None,
            max_concurrent_fn=lambda cfg_obj: cfg_obj.runtime.max_concurrent,
            worker_factory=OverrideWorker,
        )
        == 5
    )
    assert worker_calls == [
        (override_cfg, "/tmp/override.yaml", {"max_concurrent": 3}),
        "run",
    ]

    assert any(name == "reserve_slot" for name, _payload in calls)
    assert any(name == "start_background_process" for name, _payload in calls)


def test_internal_engine_queue_module_create_from_definition_uses_queue_contract(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue-root"
    queue_root.mkdir()
    entry = SimpleNamespace(queue_id="queue-1", status=SimpleNamespace(value="pending"))
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(tmp_path),
            admission_root=str(tmp_path / "admission"),
            admission_limit=1,
            max_concurrent=1,
        )
    )
    started_commands: list[list[str]] = []

    def start_background_process(command: list[str]) -> str:
        started_commands.append(command)
        return "process"

    definition = EngineDefinition(
        engine="demo",
        load_config=lambda config_path: cfg if config_path == "/tmp/config.yaml" else None,
        run_worker_child_job=lambda **_kwargs: 0,
        queue_worker_module="orca_auto.core.engines.queue_worker",
        worker_pid_file_name="definition_worker.pid",
        build_worker_child_command=lambda **_kwargs: ["unused"],
        queue_functions=EngineQueueFunctions(
            runtime_roots_for_cfg=lambda _cfg: (queue_root,),
            list_queue=lambda root: [entry] if Path(root) == queue_root else [],
            dequeue_next=lambda root: entry if Path(root) == queue_root else None,
            worker_pid_file_name="queue_functions_worker.pid",
        ),
    )
    deps = InternalEngineQueueWorkerDeps(
        time_module=SimpleNamespace(sleep=lambda _seconds: None),
        release_slot=lambda _root, _token: None,
        reserve_slot=lambda *_args, **_kwargs: "slot-1",
        start_background_process=start_background_process,
        build_worker_child_command=lambda **kwargs: ["worker", kwargs["queue_id"]],
        config_path_for_worker=lambda args, *, default_config_path_fn: (
            args.config or default_config_path_fn()
        ),
        default_config_path=lambda: "/tmp/default.yaml",
        activate_reserved_slot=lambda *_args, **_kwargs: object(),
        terminate_process=lambda _process: None,
        mark_failed=lambda *_args, **_kwargs: None,
        handle_worker_start_error=lambda *_args, **_kwargs: None,
        finalize_completed_job=lambda *_args, **_kwargs: None,
        finalize_child_exit=lambda *_args, **_kwargs: None,
        reconcile_worker_state=lambda _worker: None,
        list_queue=lambda _root: [entry],
        list_slots=lambda _root: [],
        reconcile_stale_slots=lambda _root: None,
        reconcile_orphaned_child_queue_entries=lambda *_args, **_kwargs: None,
        mark_cancelled=lambda *_args, **_kwargs: None,
        requeue_running_entry=lambda *_args, **_kwargs: None,
        mark_recovery_pending=lambda *_args, **_kwargs: None,
    )

    module = InternalEngineQueueModule.create_from_definition(
        definition=definition,
        spec=InternalEngineSpec(
            engine="demo",
            worker_job_module="orca_auto.demo.worker_execution",
            worker_pid_file_name="spec_worker.pid",
        ),
        poll_interval_seconds=5,
        shutdown_grace_seconds=1.0,
        deps=deps,
    )

    assert module.runtime.runtime.worker_pid_file_name == "queue_functions_worker.pid"
    assert module.queue_roots(cfg) == (queue_root,)
    assert module.dequeue_next_entry(cfg) == (queue_root, entry)
    assert module.try_reserve_admission_slot(cfg) == "slot-1"
    assert (
        module.start_background_job_process(
            config_path="/tmp/config.yaml",
            queue_root=queue_root,
            entry=entry,
            admission_root=tmp_path / "admission",
            admission_token="slot-1",
        )
        == "process"
    )
    assert started_commands == [["worker", "queue-1"]]
    assert module.config_path_for_worker(SimpleNamespace(config="")) == "/tmp/default.yaml"


def test_internal_engine_queue_module_create_from_definition_accepts_queue_overrides(
    tmp_path: Path,
) -> None:
    default_root = tmp_path / "default-root"
    override_root = tmp_path / "override-root"
    default_root.mkdir()
    override_root.mkdir()
    default_entry = SimpleNamespace(queue_id="default", status=SimpleNamespace(value="pending"))
    override_entry = SimpleNamespace(queue_id="override", status=SimpleNamespace(value="pending"))
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(tmp_path),
            admission_root=str(tmp_path / "admission"),
            admission_limit=1,
            max_concurrent=1,
        )
    )
    definition = EngineDefinition(
        engine="demo",
        load_config=lambda _config_path: cfg,
        run_worker_child_job=lambda **_kwargs: 0,
        queue_worker_module="orca_auto.core.engines.queue_worker",
        worker_pid_file_name="definition_worker.pid",
        build_worker_child_command=lambda **_kwargs: ["worker"],
        queue_functions=EngineQueueFunctions(
            runtime_roots_for_cfg=lambda _cfg: (default_root,),
            list_queue=lambda _root: [default_entry],
            dequeue_next=lambda _root: default_entry,
            worker_pid_file_name="queue_functions_worker.pid",
        ),
    )
    deps = InternalEngineQueueWorkerDeps(
        time_module=SimpleNamespace(sleep=lambda _seconds: None),
        release_slot=lambda _root, _token: None,
        reserve_slot=lambda *_args, **_kwargs: "slot-1",
        start_background_process=lambda _command: "process",
        build_worker_child_command=lambda **_kwargs: ["worker"],
        config_path_for_worker=lambda args, *, default_config_path_fn: (
            args.config or default_config_path_fn()
        ),
        default_config_path=lambda: "/tmp/default.yaml",
        activate_reserved_slot=lambda *_args, **_kwargs: object(),
        terminate_process=lambda _process: None,
        mark_failed=lambda *_args, **_kwargs: None,
        handle_worker_start_error=lambda *_args, **_kwargs: None,
        finalize_completed_job=lambda *_args, **_kwargs: None,
        finalize_child_exit=lambda *_args, **_kwargs: None,
        reconcile_worker_state=lambda _worker: None,
        list_queue=lambda _root: [override_entry],
        list_slots=lambda _root: [],
        reconcile_stale_slots=lambda _root: None,
        reconcile_orphaned_child_queue_entries=lambda *_args, **_kwargs: None,
        mark_cancelled=lambda *_args, **_kwargs: None,
        requeue_running_entry=lambda *_args, **_kwargs: None,
        mark_recovery_pending=lambda *_args, **_kwargs: None,
    )

    module = InternalEngineQueueModule.create_from_definition(
        definition=definition,
        spec=InternalEngineSpec(
            engine="demo",
            worker_job_module="orca_auto.demo.worker_execution",
            worker_pid_file_name="spec_worker.pid",
        ),
        poll_interval_seconds=5,
        shutdown_grace_seconds=1.0,
        deps=deps,
        runtime_roots_for_cfg=lambda _cfg: (override_root,),
        list_queue=lambda _root: [override_entry],
        dequeue_next=lambda _root: override_entry,
    )

    assert module.runtime.runtime.worker_pid_file_name == "queue_functions_worker.pid"
    assert module.queue_roots(cfg) == (override_root,)
    assert module.queue_entries_with_roots(cfg) == [(override_root, override_entry)]
    assert module.dequeue_next_entry(cfg) == (override_root, override_entry)
