from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.engines import (
    entry_matches_engine_identity,
    own_engine_accept_entry,
)
from orca_auto.core.engines.definitions import (
    EngineDefinition,
    EngineQueueFunctions,
    EngineRunnerCallbacks,
)
from orca_auto.core.queue.engine.runtime import EngineQueueRuntime
from orca_auto.core.queue.worker.execution_dependencies import (
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


def _internal_entry(engine: str, queue_id: str) -> SimpleNamespace:
    task_kind = {
        "orca": "orca_run_inp",
        "crest": "crest_conformer_search",
        "xtb": "xtb_opt",
    }.get(engine, f"{engine}_run")
    return SimpleNamespace(
        queue_id=queue_id,
        app_name=f"orca_auto_{engine}",
        task_id=f"{engine}-task-{queue_id}",
        task_kind=task_kind,
        engine=engine,
        metadata={"job_type": "opt"} if engine == "xtb" else {},
        status=SimpleNamespace(value="pending"),
    )


def test_engine_identity_rejects_conflicting_present_labels() -> None:
    accept_xtb = own_engine_accept_entry("xtb")

    own_entry = SimpleNamespace(
        queue_id="q-xtb",
        app_name="orca_auto_xtb",
        task_id="xtb-1",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={"job_type": "opt"},
    )
    assert accept_xtb(own_entry)

    for overrides in (
        {"app_name": "orca_auto_crest"},
        {"engine": "crest"},
        {"queue_id": ""},
        {"task_id": ""},
        {"task_kind": ""},
        {"task_kind": "crest_conformer_search"},
        {"app_name": "", "engine": ""},
    ):
        assert not accept_xtb(SimpleNamespace(**{**vars(own_entry), **overrides}))

    assert not entry_matches_engine_identity(
        SimpleNamespace(
            queue_id="q-crest-legacy",
            app_name="orca_auto_crest",
            task_id="crest-1",
            task_kind="conformer_search",
            engine="crest",
        ),
        "crest",
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


def test_engine_definition_builds_canonical_runtime_from_queue_contract(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    own_entry = _internal_entry("demo", "queue-own")
    foreign_entry = _internal_entry("crest", "queue-foreign")
    looked_up: list[tuple[Path | str, str]] = []

    def queue_entry_by_id(root: Path | str, queue_id: str) -> Any | None:
        looked_up.append((root, queue_id))
        return own_entry if queue_id == own_entry.queue_id else foreign_entry

    definition = EngineDefinition(
        engine="demo",
        load_config=lambda path: path,
        queue_worker_module="orca_auto.core.engines.queue_worker",
        queue_functions=EngineQueueFunctions(
            runtime_roots_for_cfg=lambda _cfg: (queue_root,),
            list_queue=lambda _root: [foreign_entry, own_entry],
            dequeue_next=lambda _root: own_entry,
            dequeue_entry_if_pending=lambda _root, _queue_id, **_kwargs: own_entry,
            queue_entry_by_id=queue_entry_by_id,
            worker_pid_file_name="queue-contract.pid",
        ),
        runner_callbacks=EngineRunnerCallbacks(
            run_worker_child_job=lambda **_kwargs: 0,
            build_worker_child_command=lambda **_kwargs: ["worker"],
        ),
    )

    runtime = definition.build_queue_runtime()

    assert runtime.worker_pid_file_name == "queue-contract.pid"
    assert runtime.queue_entries_with_roots(object()) == [(queue_root, own_entry)]
    assert runtime.dequeue_next_entry(object()) == (queue_root, own_entry)
    assert runtime.queue_entry_by_id(queue_root, own_entry.queue_id) is own_entry
    assert runtime.queue_entry_by_id(queue_root, foreign_entry.queue_id) is None
    assert looked_up == [
        (queue_root, own_entry.queue_id),
        (queue_root, foreign_entry.queue_id),
    ]


def test_engine_definition_requires_worker_pid_in_queue_contract() -> None:
    definition = EngineDefinition(
        engine="demo",
        load_config=lambda path: path,
        queue_worker_module="orca_auto.core.engines.queue_worker",
        queue_functions=EngineQueueFunctions(
            runtime_roots_for_cfg=lambda _cfg: (),
            list_queue=lambda _root: [],
            dequeue_next=lambda _root: None,
        ),
        runner_callbacks=EngineRunnerCallbacks(
            run_worker_child_job=lambda **_kwargs: 0,
            build_worker_child_command=lambda **_kwargs: ["worker"],
        ),
    )

    with pytest.raises(ValueError, match="worker_pid_file_name is required"):
        definition.build_queue_runtime()


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
                "engine_process_state": "idle",
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
