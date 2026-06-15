from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orca_auto.core.queue.child_process import reconcile_orphaned_child_queue_entries
from orca_auto.core.queue.internal_engine import (
    InternalEngineQueueRuntime,
    InternalEngineQueueWorkerFacade,
    InternalEngineQueueWorkerFacadeBindings,
    InternalEngineQueueWorkerFacadeCallbacks,
    InternalEngineSpec,
    build_internal_engine_queue_worker_deps,
    build_late_bound_internal_engine_queue_worker_deps,
)
from orca_auto.core.queue.types import QueueStatus


def _facade_deps(*, time_module: Any | None = None, **overrides: Any) -> Any:
    callback_values: dict[str, Any] = {
        "release_slot": lambda *_args, **_kwargs: None,
        "reserve_slot": lambda *_args, **_kwargs: "slot-1",
        "start_background_process": lambda _command: "process",
        "build_worker_child_command": lambda **kwargs: ["worker", kwargs["queue_id"]],
        "config_path_for_worker": lambda args, *, default_config_path_fn: (
            getattr(args, "config", "") or default_config_path_fn()
        ),
        "default_config_path": lambda: "/tmp/default.yaml",
        "activate_reserved_slot": lambda *_args, **_kwargs: object(),
        "terminate_process": lambda _process: None,
        "mark_failed": lambda *_args, **_kwargs: None,
        "handle_worker_start_error": lambda *_args, **_kwargs: None,
        "finalize_completed_job": lambda *_args, **_kwargs: None,
        "finalize_child_exit": lambda *_args, **_kwargs: None,
        "reconcile_worker_state": lambda *_args, **_kwargs: None,
        "list_queue": lambda _root: [],
        "list_slots": lambda _root: [],
        "reconcile_stale_slots": lambda _root: None,
        "mark_cancelled": lambda *_args, **_kwargs: None,
        "requeue_running_entry": lambda *_args, **_kwargs: None,
    }
    callback_values.update(overrides)
    return build_internal_engine_queue_worker_deps(
        InternalEngineQueueWorkerFacadeCallbacks(**callback_values),
        time_module=time_module or SimpleNamespace(sleep=lambda _seconds: None),
    )


def test_internal_engine_lifecycle_policy_preserves_roots_and_recovers_job_entry(
    tmp_path: Path,
) -> None:
    cfg = object()
    current_entry = SimpleNamespace(queue_id="queue-1", status=QueueStatus.RUNNING)
    job_entry = SimpleNamespace(queue_id="original", status=QueueStatus.RUNNING)
    job = SimpleNamespace(
        queue_root=tmp_path / "queue",
        entry=job_entry,
        admission_token="slot-1",
    )
    requeued: list[tuple[Path, str]] = []
    recovery: list[tuple[object, object, str]] = []
    released: list[str] = []

    InternalEngineSpec(engine="xtb").lifecycle().finalize_child_exit(
        cfg,
        job,
        rc=0,
        shutdown_requested=True,
        find_queue_entry_fn=lambda _root, _queue_id: current_entry,
        mark_cancelled_fn=lambda *args, **kwargs: None,
        requeue_running_entry_fn=lambda root, queue_id: requeued.append((root, queue_id)),
        mark_failed_fn=lambda *args, **kwargs: None,
        mark_recovery_pending_fn=lambda cfg_obj, entry_obj, *, reason: recovery.append(
            (cfg_obj, entry_obj, reason)
        ),
        release_admission_slot_fn=lambda token: released.append(token),
    )

    assert requeued == [(tmp_path / "queue", "queue-1")]
    assert recovery == [(cfg, job_entry, "worker_shutdown")]
    assert released == ["slot-1"]


def test_internal_engine_admission_uses_engine_identity() -> None:
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            admission_root="/tmp/admission",
            admission_limit=2,
        )
    )
    calls: list[tuple[str, int, str, str]] = []

    def reserve_slot(root: str, limit: int, *, source: str, app_name: str) -> str:
        calls.append((root, limit, source, app_name))
        return "slot-1"

    token = (
        InternalEngineSpec(engine="demo-engine")
        .admission()
        .reserve_admission_slot(
            cfg,
            reserve_slot_fn=reserve_slot,
        )
    )

    assert token == "slot-1"
    assert calls == [
        ("/tmp/admission", 2, "orca_auto.demo_engine.queue_worker", "orca_auto_demo_engine")
    ]


def test_internal_engine_queue_worker_facade_runs_worker_command_with_deps(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(tmp_path),
            max_concurrent=3,
        )
    )
    runtime = InternalEngineQueueRuntime.create(
        spec=InternalEngineSpec(engine="demo", worker_pid_file_name="demo_worker.pid"),
        load_config=lambda _path: cfg,
        runtime_roots_for_cfg=lambda _cfg: (),
        list_queue=lambda _root: [],
        dequeue_next=lambda _root: None,
    )
    seen: list[tuple[object, str, int]] = []

    class Worker:
        def __init__(
            self,
            cfg_arg: object,
            *,
            config_path: str,
            max_concurrent: int,
        ) -> None:
            seen.append((cfg_arg, config_path, max_concurrent))

        def run(self) -> int:
            return 19

    facade = InternalEngineQueueWorkerFacade(
        runtime=runtime,
        deps=_facade_deps(
            load_config=lambda _path: cfg,
            read_worker_pid=lambda _root: None,
            worker_class=Worker,
        ),
        poll_interval_seconds=5,
        shutdown_grace_seconds=10,
    )

    result = facade.run_pidfile_worker_command(
        SimpleNamespace(config="/tmp/demo.yaml"),
        config_path_fn=lambda args: args.config,
    )

    assert result == 19
    assert seen == [(cfg, "/tmp/demo.yaml", 3)]


def test_internal_engine_queue_worker_facade_uses_late_bound_bindings(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root=str(tmp_path),
            admission_root="/tmp/admission",
            admission_limit=2,
            max_concurrent=1,
        )
    )
    runtime = InternalEngineQueueRuntime.create(
        spec=InternalEngineSpec(engine="demo", worker_pid_file_name="demo_worker.pid"),
        load_config=lambda _path: cfg,
        runtime_roots_for_cfg=lambda _cfg: (),
        list_queue=lambda _root: [],
        dequeue_next=lambda _root: None,
    )
    started_commands: list[list[str]] = []

    def start_background_process(command: list[str]) -> str:
        started_commands.append(command)
        return "process"

    reserve_slot_token = {"value": "old-token"}

    def reserve_slot_fn(*_args: Any, **_kwargs: Any) -> str:
        return reserve_slot_token["value"]

    deps = build_late_bound_internal_engine_queue_worker_deps(
        InternalEngineQueueWorkerFacadeBindings(
            release_slot=lambda: lambda *_args: None,
            reserve_slot=lambda: reserve_slot_fn,
            start_background_process=lambda: start_background_process,
            build_worker_child_command=lambda: lambda **kwargs: ["worker", kwargs["queue_id"]],
            config_path_for_worker=lambda: (
                lambda args, *, default_config_path_fn: args.config or default_config_path_fn()
            ),
            default_config_path=lambda: lambda: "/tmp/default.yaml",
            activate_reserved_slot=lambda: lambda *_args, **_kwargs: object(),
            terminate_process=lambda: lambda _process: None,
            mark_failed=lambda: lambda *_args, **_kwargs: None,
            handle_worker_start_error=lambda: lambda *_args, **_kwargs: None,
            finalize_completed_job=lambda: lambda *_args, **_kwargs: None,
            finalize_child_exit=lambda: lambda *_args, **_kwargs: None,
            reconcile_worker_state=lambda: lambda *_args, **_kwargs: None,
            list_queue=lambda: lambda _root: [],
            list_slots=lambda: lambda _root: [],
            reconcile_stale_slots=lambda: lambda _root: None,
            mark_cancelled=lambda: lambda *_args, **_kwargs: None,
            requeue_running_entry=lambda: lambda *_args, **_kwargs: None,
        ),
        time_module=SimpleNamespace(sleep=lambda _seconds: None),
    )
    facade = InternalEngineQueueWorkerFacade(
        runtime=runtime,
        deps=deps,
        poll_interval_seconds=5,
        shutdown_grace_seconds=10,
    )

    reserve_slot_token["value"] = "new-token"

    assert facade.try_reserve_admission_slot(cfg) == "new-token"
    assert facade.config_path_for_worker(SimpleNamespace(config="")) == "/tmp/default.yaml"
    assert (
        facade.start_background_job_process(
            config_path="/tmp/cfg.yaml",
            queue_root=tmp_path / "queue",
            entry=SimpleNamespace(queue_id="queue-1"),
            admission_root="/tmp/admission",
            admission_token="slot-1",
        )
        == "process"
    )
    assert started_commands == [["worker", "queue-1"]]


def test_internal_engine_queue_worker_facade_finalizes_child_exit(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(tmp_path)))
    current_entry = SimpleNamespace(queue_id="queue-1", status=QueueStatus.RUNNING)
    job_entry = SimpleNamespace(queue_id="queue-1", status=QueueStatus.RUNNING)
    job = SimpleNamespace(
        queue_root=tmp_path / "queue",
        entry=job_entry,
        admission_token="slot-1",
    )
    requeued: list[tuple[Path, str]] = []
    recovery: list[tuple[object, object, str]] = []
    released: list[str] = []
    runtime = InternalEngineQueueRuntime.create(
        spec=InternalEngineSpec(
            engine="demo",
            worker_pid_file_name="demo_worker.pid",
        ),
        load_config=lambda _path: cfg,
        runtime_roots_for_cfg=lambda _cfg: (tmp_path / "queue",),
        list_queue=lambda _root: [current_entry],
        dequeue_next=lambda _root: None,
    )
    facade = InternalEngineQueueWorkerFacade(
        runtime=runtime,
        deps=_facade_deps(
            find_queue_entry=lambda _root, _queue_id: current_entry,
            mark_cancelled=lambda *_args, **_kwargs: None,
            requeue_running_entry=lambda root, queue_id: requeued.append((root, queue_id)),
            mark_failed=lambda *_args, **_kwargs: None,
            mark_recovery_pending=lambda cfg_obj, entry_obj, *, reason: recovery.append(
                (cfg_obj, entry_obj, reason)
            ),
        ),
        poll_interval_seconds=5,
        shutdown_grace_seconds=10,
    )
    worker = SimpleNamespace(
        cfg=cfg,
        _shutdown_requested=True,
        _release_admission_slot=lambda token: released.append(token),
    )

    facade.finalize_child_exit(worker, job, rc=0)

    assert requeued == [(tmp_path / "queue", "queue-1")]
    assert recovery == [(cfg, job_entry, "worker_shutdown")]
    assert released == ["slot-1"]


def test_internal_engine_queue_worker_facade_reconciles_orphaned_running(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(tmp_path)))
    live_entry = SimpleNamespace(queue_id="live", status=QueueStatus.RUNNING)
    orphan_entry = SimpleNamespace(queue_id="orphan", status=QueueStatus.RUNNING)
    queue_root = tmp_path / "queue"
    requeued: list[tuple[Path, str]] = []
    recovery: list[tuple[object, object, str]] = []
    runtime = InternalEngineQueueRuntime.create(
        spec=InternalEngineSpec(
            engine="demo",
            worker_pid_file_name="demo_worker.pid",
        ),
        load_config=lambda _path: cfg,
        runtime_roots_for_cfg=lambda _cfg: (queue_root,),
        list_queue=lambda _root: [live_entry, orphan_entry],
        dequeue_next=lambda _root: None,
    )
    facade = InternalEngineQueueWorkerFacade(
        runtime=runtime,
        deps=_facade_deps(
            list_queue=lambda _root: [live_entry, orphan_entry],
            list_slots=lambda _root: [SimpleNamespace(queue_id="live")],
            reconcile_stale_slots=lambda _root: None,
            reconcile_orphaned_child_queue_entries=reconcile_orphaned_child_queue_entries,
            mark_cancelled=lambda *_args, **_kwargs: None,
            requeue_running_entry=lambda root, queue_id: requeued.append((root, queue_id)),
            mark_failed=lambda *_args, **_kwargs: None,
            mark_recovery_pending=lambda cfg_obj, entry_obj, *, reason: recovery.append(
                (cfg_obj, entry_obj, reason)
            ),
        ),
        poll_interval_seconds=5,
        shutdown_grace_seconds=10,
    )
    worker = SimpleNamespace(cfg=cfg, admission_root=tmp_path / "admission")

    facade.reconcile_orphaned_running(worker)

    assert requeued == [(queue_root, "orphan")]
    assert recovery == [(cfg, orphan_entry, "crashed_recovery")]


def test_internal_engine_worker_child_merges_default_and_explicit_process_kwargs(
    tmp_path: Path,
) -> None:
    cfg = SimpleNamespace(name="cfg")
    entry = SimpleNamespace(queue_id="queue-1", status=QueueStatus.RUNNING)
    processed: list[dict[str, Any]] = []
    child = InternalEngineSpec(
        engine="demo",
        worker_job_module="orca_auto.demo.worker_execution",
    ).worker_child(
        RuntimeError,
        process_dequeued_entry_kwargs_fn=lambda: {
            "engine_default": "default",
            "overridden": "default",
        },
    )

    rc = child.run_worker_child_job(
        config_path="/tmp/demo.yaml",
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
        overridden="explicit",
        call_only="extra",
    )

    assert rc == 0
    kwargs = processed[0]["kwargs"]
    assert kwargs["engine_default"] == "default"
    assert kwargs["overridden"] == "explicit"
    assert kwargs["call_only"] == "extra"
