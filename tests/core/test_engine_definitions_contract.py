from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from orca_auto.core.engines import (
    build_lazy_queue_worker_runner,
    build_lazy_worker_child_runner,
    build_queue_engine_definition,
    build_worker_child_command_for_engine,
)
from orca_auto.core.engines.definitions import (
    EngineDefinition,
    EngineRunnerCallbacks,
)
from orca_auto.core.engines.queue_worker import (
    build_engine_queue_worker,
    build_runtime_engine_queue_worker,
    run_engine_queue_worker,
)
from orca_auto.core.engines.worker_child import run_engine_worker_child_job
from orca_auto.core.queue.internal_engine_worker_deps import InternalEngineQueueWorkerDeps


def _definition(**overrides: Any) -> EngineDefinition:
    values: dict[str, Any] = {
        "engine": "demo",
        "load_config": lambda path: path,
        "run_worker_child_job": lambda **_kwargs: 0,
        "queue_worker_module": "orca_auto.demo.queue",
        "worker_pid_file_name": "demo_worker.pid",
        "build_worker_child_command": lambda **_kwargs: ["demo-child"],
    }
    values.update(overrides)
    return EngineDefinition(**values)


def test_engine_queue_worker_dispatches_definition_runner(monkeypatch: Any) -> None:
    calls: list[list[str]] = []

    def _runner(argv: list[str]) -> int:
        calls.append(argv)
        return 7

    definition = _definition(queue_worker_runner=_runner)

    from orca_auto.core.engines import registry

    monkeypatch.setattr(registry, "get_engine_definition", lambda _engine: definition)

    assert run_engine_queue_worker("demo", ["--config", "/tmp/config.yaml"]) == 7
    assert calls == [["--config", "/tmp/config.yaml"]]


def test_build_engine_queue_worker_forwards_common_callbacks(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class FakeEngineQueueWorker:
        def __init__(self, cfg: Any, **kwargs: Any) -> None:
            captured["cfg"] = cfg
            captured["kwargs"] = kwargs

    from orca_auto.core.engines import queue_worker

    monkeypatch.setattr(queue_worker, "EngineQueueWorker", FakeEngineQueueWorker)
    cfg = object()
    worker = build_engine_queue_worker(
        cfg,
        config_path="/tmp/config.yaml",
        engine="demo",
        max_concurrent=2,
        deps="deps",
        hooks="hooks",
        worker_pid_file_name="demo.pid",
        admission_root="/tmp/admission",
        auto_organize=True,
        running_queue_id=lambda entry: entry.queue_id,
        finalize_finished_job=lambda *_args, **_kwargs: None,
        finalize_child_exit=lambda *_args, **_kwargs: None,
        reconcile_orphaned_running=lambda *_args, **_kwargs: None,
        check_cancel_requests=lambda *_args, **_kwargs: None,
    )

    assert isinstance(worker, FakeEngineQueueWorker)
    assert captured["cfg"] is cfg
    assert captured["kwargs"]["engine"] == "demo"
    assert captured["kwargs"]["config_path"] == "/tmp/config.yaml"
    assert captured["kwargs"]["max_concurrent"] == 2
    assert captured["kwargs"]["worker_pid_file_name"] == "demo.pid"
    assert captured["kwargs"]["admission_root"] == "/tmp/admission"
    assert captured["kwargs"]["auto_organize"] is True
    assert callable(captured["kwargs"]["running_queue_id"])
    assert callable(captured["kwargs"]["finalize_finished_job"])
    assert callable(captured["kwargs"]["check_cancel_requests"])


def test_build_runtime_engine_queue_worker_resolves_defaults_and_max_concurrency() -> None:
    calls: list[dict[str, Any]] = []

    def worker_builder(cfg: Any, **kwargs: Any) -> Any:
        calls.append({"cfg": cfg, **kwargs})
        return "worker"

    cfg = SimpleNamespace(runtime=SimpleNamespace(max_concurrent=0))

    assert (
        build_runtime_engine_queue_worker(
            cfg,
            config_path="",
            default_config_path=lambda: "/tmp/default.yaml",
            engine="crest",
            max_concurrent=None,
            deps="deps",
            hooks="hooks",
            worker_pid_file_name="worker.pid",
            admission_root="/tmp/admission",
            normalize_max_concurrent=True,
            worker_builder=worker_builder,
        )
        == "worker"
    )

    assert (
        build_runtime_engine_queue_worker(
            cfg,
            config_path="/tmp/explicit.yaml",
            default_config_path=lambda: "/tmp/default.yaml",
            engine="xtb",
            max_concurrent=None,
            deps="deps",
            hooks="hooks",
            worker_pid_file_name="worker.pid",
            admission_root="/tmp/admission",
            worker_builder=worker_builder,
        )
        == "worker"
    )

    assert calls[0]["config_path"] == "/tmp/default.yaml"
    assert calls[0]["max_concurrent"] == 1
    assert calls[1]["config_path"] == "/tmp/explicit.yaml"
    assert calls[1]["max_concurrent"] is None


def test_orca_queue_worker_uses_common_builder(monkeypatch: Any) -> None:
    from orca_auto.orca import queue_worker as orca_queue_worker

    captured: dict[str, Any] = {}
    fake_worker = SimpleNamespace()

    def fake_build_engine_queue_worker(cfg: Any, **kwargs: Any) -> Any:
        captured["cfg"] = cfg
        captured["kwargs"] = kwargs
        return fake_worker

    cfg: Any = SimpleNamespace(runtime=SimpleNamespace(admission_limit=None, max_concurrent=1))
    monkeypatch.setattr(
        orca_queue_worker, "build_engine_queue_worker", fake_build_engine_queue_worker
    )
    monkeypatch.setattr(orca_queue_worker, "_queue_worker_deps", lambda: "deps")
    monkeypatch.setattr(orca_queue_worker, "_queue_worker_hooks", lambda: "hooks")
    monkeypatch.setattr(orca_queue_worker, "_admission_root_for_cfg", lambda _cfg: "/tmp/admission")

    worker = orca_queue_worker.QueueWorker(
        cfg,
        "/tmp/config.yaml",
        max_concurrent=0,
        auto_organize=True,
    )

    assert worker is fake_worker
    assert captured["cfg"] is not cfg
    assert captured["cfg"].runtime is not cfg.runtime
    assert captured["cfg"].runtime.max_concurrent == 1
    assert cfg.runtime.max_concurrent == 1
    kwargs = captured["kwargs"]
    assert kwargs["engine"] == "orca"
    assert kwargs["config_path"] == "/tmp/config.yaml"
    assert kwargs["max_concurrent"] == 1
    assert kwargs["deps"] == "deps"
    assert kwargs["hooks"] == "hooks"
    assert kwargs["worker_pid_file_name"] == orca_queue_worker.WORKER_PID_FILE
    assert kwargs["admission_root"] == "/tmp/admission"
    assert kwargs["auto_organize"] is True
    assert kwargs["running_queue_id"] is orca_queue_worker.queue_entry_id
    assert callable(kwargs["running_job_factory"])
    assert callable(kwargs["finalize_finished_job"])
    assert callable(kwargs["reconcile_orphaned_running"])
    assert callable(kwargs["check_cancel_requests"])
    assert callable(vars(worker)["_auto_organize_terminal_job"])
    assert callable(vars(worker)["_cancel_running_job"])


def test_crest_runtime_facade_deps_use_late_bound_callbacks(monkeypatch: Any) -> None:
    from orca_auto.flow.engines.crest import queue_runtime as crest_queue_runtime

    reserve_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    list_calls: list[str] = []

    def reserve_slot(*args: Any, **kwargs: Any) -> str:
        reserve_calls.append((args, kwargs))
        return "slot-1"

    def list_queue(root: str | Path) -> list[Any]:
        list_calls.append(str(root))
        return ["entry"]

    monkeypatch.setattr(crest_queue_runtime, "reserve_slot", reserve_slot)
    monkeypatch.setattr(crest_queue_runtime, "list_queue", list_queue)

    deps = crest_queue_runtime._runtime_facade_deps()

    assert isinstance(deps, InternalEngineQueueWorkerDeps)
    assert deps.reserve_slot("/tmp/admission", 2, source="test") == "slot-1"
    assert reserve_calls == [(("/tmp/admission", 2), {"source": "test"})]
    assert deps.list_queue("/tmp/queue") == ["entry"]
    assert list_calls == ["/tmp/queue"]


def test_xtb_runtime_facade_deps_use_late_bound_callbacks(monkeypatch: Any) -> None:
    from orca_auto.flow.engines.xtb import queue_runtime as xtb_queue_runtime

    reserve_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    list_calls: list[str] = []

    def reserve_slot(*args: Any, **kwargs: Any) -> str:
        reserve_calls.append((args, kwargs))
        return "slot-1"

    def list_queue(root: str | Path) -> list[Any]:
        list_calls.append(str(root))
        return ["entry"]

    monkeypatch.setattr(xtb_queue_runtime, "reserve_slot", reserve_slot)
    monkeypatch.setattr(xtb_queue_runtime, "list_queue", list_queue)

    deps = xtb_queue_runtime._runtime_facade_deps()

    assert isinstance(deps, InternalEngineQueueWorkerDeps)
    assert deps.reserve_slot("/tmp/admission", 2, source="test") == "slot-1"
    assert reserve_calls == [(("/tmp/admission", 2), {"source": "test"})]
    assert deps.list_queue("/tmp/queue") == ["entry"]
    assert list_calls == ["/tmp/queue"]


def test_orca_runtime_facade_deps_use_late_bound_callbacks(monkeypatch: Any) -> None:
    from orca_auto.orca import queue_worker as orca_queue_worker

    reserve_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    list_calls: list[str] = []

    def reserve_slot(*args: Any, **kwargs: Any) -> str:
        reserve_calls.append((args, kwargs))
        return "slot-1"

    def list_queue(root: str | Path) -> list[Any]:
        list_calls.append(str(root))
        return ["entry"]

    monkeypatch.setattr(orca_queue_worker, "_reserve_orca_worker_slot", reserve_slot)
    monkeypatch.setattr(orca_queue_worker, "_list_queue_for_runtime", list_queue)

    deps = orca_queue_worker._runtime_facade_deps()

    assert isinstance(deps, InternalEngineQueueWorkerDeps)
    assert deps.reserve_slot("/tmp/admission", 2, work_dir="/tmp/job") == "slot-1"
    assert reserve_calls == [(("/tmp/admission", 2), {"work_dir": "/tmp/job"})]
    assert deps.list_queue("/tmp/queue") == ["entry"]
    assert list_calls == ["/tmp/queue"]
    assert deps.default_config_path() == ""
    assert (
        deps.config_path_for_worker(
            SimpleNamespace(config="/tmp/config.yaml"),
            default_config_path_fn=deps.default_config_path,
        )
        == "/tmp/config.yaml"
    )


def test_build_worker_child_command_for_engine_adds_engine_argument() -> None:
    command = build_worker_child_command_for_engine("Demo")(
        config_path="/tmp/config.yaml",
        queue_root=Path("/tmp/queue"),
        queue_id="qid",
        admission_token="token",
        admission_root=Path("/tmp/admission"),
    )

    assert command[1:3] == ["-m", "orca_auto.core.engines.worker_child"]
    assert "--engine" in command
    assert command[command.index("--engine") + 1] == "demo"
    assert command[command.index("--admission-token") + 1] == "token"


def test_lazy_engine_definition_runners_dispatch_to_current_module_function(
    monkeypatch: Any,
) -> None:
    module = ModuleType("orca_auto.tests.lazy_engine_runtime")
    calls: list[tuple[str, Any]] = []

    def run_worker_child_job(**kwargs: Any) -> int:
        calls.append(("child", kwargs))
        return 11

    def main(argv: list[str]) -> int:
        calls.append(("main", argv))
        return 13

    module.__dict__["run_worker_child_job"] = run_worker_child_job
    module.__dict__["main"] = main
    monkeypatch.setitem(sys.modules, module.__name__, module)

    child_runner = build_lazy_worker_child_runner(
        module.__name__,
        "run_worker_child_job",
    )
    queue_runner = build_lazy_queue_worker_runner(module.__name__)

    assert (
        child_runner(
            config_path="/tmp/config.yaml",
            queue_root=Path("/tmp/queue"),
            queue_id="queue-1",
            admission_token="slot-1",
            ignored="value",
        )
        == 11
    )
    assert queue_runner(["--config", "/tmp/config.yaml"]) == 13
    assert calls == [
        (
            "child",
            {
                "config_path": "/tmp/config.yaml",
                "queue_root": Path("/tmp/queue"),
                "queue_id": "queue-1",
                "admission_token": "slot-1",
            },
        ),
        ("main", ["--config", "/tmp/config.yaml"]),
    ]

    def replacement_child(**_kwargs: Any) -> int:
        return 17

    module.__dict__["run_worker_child_job"] = replacement_child
    assert (
        child_runner(
            config_path="/tmp/next.yaml",
            queue_root="/tmp/queue",
            queue_id="queue-2",
        )
        == 17
    )

    def replacement_main(_argv: list[str]) -> int:
        return 19

    module.__dict__["main"] = replacement_main
    assert queue_runner([]) == 19


def test_build_queue_engine_definition_wires_common_contracts(tmp_path: Path) -> None:
    entry = SimpleNamespace(queue_id="queue-1")
    listed_roots: list[Path] = []
    started_calls: list[dict[str, Any]] = []

    def _list_queue(root: str | Path) -> list[Any]:
        listed_roots.append(Path(root))
        return [entry]

    def _dequeue_next(_root: Path) -> Any | None:
        return None

    def _run_child(**_kwargs: Any) -> int:
        return 3

    def _queue_main(_argv: list[str]) -> int:
        return 4

    def _build_child(**_kwargs: Any) -> list[str]:
        return ["child"]

    def _job_started(**kwargs: Any) -> None:
        started_calls.append(kwargs)

    definition = build_queue_engine_definition(
        engine="Demo",
        load_config=lambda path: path,
        run_worker_child_job=_run_child,
        queue_worker_runner=_queue_main,
        build_worker_child_command=_build_child,
        list_queue=_list_queue,
        dequeue_next=_dequeue_next,
        worker_pid_file_name="demo.pid",
        runtime_roots_for_cfg=lambda _cfg: (tmp_path,),
        job_started=_job_started,
    )

    assert definition.engine == "demo"
    assert definition.queue_worker_module == "orca_auto.core.engines.queue_worker"
    assert definition.worker_pid_file_name == "demo.pid"
    runtime_roots_for_cfg = definition.runtime_roots_for_cfg
    assert runtime_roots_for_cfg is not None
    assert runtime_roots_for_cfg(object()) == (tmp_path,)

    queue_functions = definition.queue_functions
    assert queue_functions is not None
    assert queue_functions.worker_pid_file_name == "demo.pid"
    assert queue_functions.runtime_roots_for_cfg(object()) == (tmp_path,)
    queue_entry_by_id = queue_functions.queue_entry_by_id
    assert queue_entry_by_id is not None
    assert queue_entry_by_id(tmp_path / "queue", "queue-1") is entry
    assert listed_roots == [tmp_path / "queue"]

    assert definition.runner_callbacks is not None
    assert definition.runner_callbacks.run_worker_child_job is _run_child
    assert definition.runner_callbacks.build_worker_child_command is _build_child
    assert definition.artifact_adapter is not None
    assert definition.notification_hooks is not None
    job_started = definition.notification_hooks.job_started
    assert job_started is not None
    assert job_started is _job_started
    job_started(engine="demo")
    assert started_calls == [{"engine": "demo"}]
    assert definition.queue_worker_main(["--config", "demo.yaml"]) == 4


def test_build_queue_engine_definition_defaults_worker_child_command(
    tmp_path: Path,
) -> None:
    definition = build_queue_engine_definition(
        engine="Demo",
        load_config=lambda path: path,
        run_worker_child_job=lambda **_kwargs: 0,
        queue_worker_runner=lambda _argv: 0,
        list_queue=lambda _root: [],
        dequeue_next=lambda _root: None,
        worker_pid_file_name="demo.pid",
    )

    command = definition.build_worker_child_command(
        config_path="/tmp/config.yaml",
        queue_root=tmp_path,
        queue_id="queue-1",
    )

    assert definition.runner_callbacks is not None
    assert definition.runner_callbacks.build_worker_child_command is (
        definition.build_worker_child_command
    )
    assert command[1:3] == ["-m", "orca_auto.core.engines.worker_child"]
    assert command[command.index("--engine") + 1] == "demo"
    assert command[command.index("--queue-id") + 1] == "queue-1"


def test_build_queue_engine_definition_defaults_core_queue_functions(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    from orca_auto.core import queue as core_queue

    entry = SimpleNamespace(queue_id="queue-1")
    list_calls: list[Path] = []
    dequeue_calls: list[Path] = []

    def list_queue(root: str | Path) -> list[Any]:
        list_calls.append(Path(root))
        return [entry]

    def dequeue_next(root: Path) -> Any | None:
        dequeue_calls.append(root)
        return entry

    monkeypatch.setattr(core_queue, "list_queue", list_queue)
    monkeypatch.setattr(core_queue, "dequeue_next", dequeue_next)

    definition = build_queue_engine_definition(
        engine="Demo",
        load_config=lambda path: path,
        run_worker_child_job=lambda **_kwargs: 0,
        queue_worker_runner=lambda _argv: 0,
        worker_pid_file_name="demo.pid",
    )

    queue_functions = definition.queue_functions
    assert queue_functions is not None
    assert queue_functions.list_queue(tmp_path) == [entry]
    assert queue_functions.dequeue_next(tmp_path) is entry
    assert queue_functions.queue_entry_by_id is not None
    assert queue_functions.queue_entry_by_id(tmp_path, "queue-1") is entry
    assert list_calls == [tmp_path, tmp_path]
    assert dequeue_calls == [tmp_path]


def test_engine_worker_child_dispatches_runner_callbacks(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    def _run_child(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 9

    definition = _definition(
        runner_callbacks=EngineRunnerCallbacks(
            run_worker_child_job=_run_child,
            build_worker_child_command=lambda **_kwargs: ["demo-child"],
        )
    )

    from orca_auto.core.engines import registry

    monkeypatch.setattr(registry, "get_engine_definition", lambda _engine: definition)

    assert (
        run_engine_worker_child_job(
            engine="demo",
            config_path="/tmp/config.yaml",
            queue_root=Path("/tmp/queue"),
            queue_id="qid",
            admission_token="token",
        )
        == 9
    )
    assert calls == [
        {
            "config_path": "/tmp/config.yaml",
            "queue_root": Path("/tmp/queue"),
            "queue_id": "qid",
            "admission_token": "token",
        }
    ]


def test_real_engine_definitions_expose_runtime_contracts() -> None:
    from orca_auto.core.engines import get_engine_definition

    for engine in ("orca", "xtb", "crest"):
        definition = get_engine_definition(engine)
        assert definition.queue_worker_module == "orca_auto.core.engines.queue_worker"
        assert definition.queue_functions is not None
        assert definition.runner_callbacks is not None
        assert definition.artifact_adapter is not None
        assert definition.notification_hooks is not None


def test_engine_specific_modules_export_common_queue_worker_factories() -> None:
    from orca_auto.flow.engines.crest import queue_runtime as crest_queue_runtime
    from orca_auto.flow.engines.xtb import queue_runtime as xtb_queue_runtime
    from orca_auto.orca import queue_worker as orca_queue_worker

    for module in (orca_queue_worker, xtb_queue_runtime, crest_queue_runtime):
        assert callable(module.QueueWorker)
        assert not inspect.isclass(module.QueueWorker)


def test_engine_queue_runtime_modules_use_definition_pid_contracts() -> None:
    from orca_auto.core.engines import get_engine_definition
    from orca_auto.flow.engines.crest import queue_runtime as crest_queue_runtime
    from orca_auto.flow.engines.xtb import queue_runtime as xtb_queue_runtime
    from orca_auto.orca import queue_worker as orca_queue_worker

    modules = {
        "orca": orca_queue_worker,
        "xtb": xtb_queue_runtime,
        "crest": crest_queue_runtime,
    }

    for engine, module in modules.items():
        definition = get_engine_definition(engine)
        assert definition.queue_functions is not None
        assert module._queue_module.runtime.runtime.worker_pid_file_name == (
            definition.queue_functions.worker_pid_file_name
        )
        assert module._queue_module.runtime.runtime.worker_pid_file_name == (
            definition.worker_pid_file_name
        )
        assert isinstance(module._runtime_facade_deps(), InternalEngineQueueWorkerDeps)
