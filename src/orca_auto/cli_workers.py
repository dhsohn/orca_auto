"""Foreground engine-worker supervision for the ``queue worker`` command."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.cli_common import (
    _discover_shared_config_path,
    _effective_shared_config_text,
    _repo_root_for_subprocess,
    _workflow_root_for_args,
)
from orca_auto.cli_errors import emit_error
from orca_auto.core.app_ids import (
    ORCA_AUTO_CONFIG_ENV_VAR,
    ORCA_AUTO_WORKFLOW_WORKER_MODULE,
)
from orca_auto.core.engine_catalog import supervised_engine_entries
from orca_auto.core.utils import normalize_text

LOGGER = logging.getLogger(__name__)

_SUPERVISED_ENGINE_ENTRIES = supervised_engine_entries()
_WORKFLOW_ENGINE_APPS = tuple(
    entry.engine_id
    for entry in _SUPERVISED_ENGINE_ENTRIES
    if entry.default_supervision_role == "with-workflow"
)
_ENGINE_APPS = tuple(
    entry.engine_id
    for entry in _SUPERVISED_ENGINE_ENTRIES
    if entry.default_supervision_role == "default"
)
_ENGINE_WORKER_MODULES = {
    entry.engine_id: entry.worker_module for entry in _SUPERVISED_ENGINE_ENTRIES
}
_KNOWN_WORKER_APPS = (*_ENGINE_APPS, "workflow")
_DEFAULT_WORKER_APPS = ("orca",)


@dataclass(frozen=True)
class WorkerSpec:
    app: str
    argv: tuple[str, ...]
    cwd: str | None = None
    env: dict[str, str] | None = None
    restart_on_clean_exit: bool = True

    def to_dict(self) -> dict[str, Any]:
        env_payload: dict[str, str] | None = None
        if isinstance(self.env, dict):
            allowed_env_keys = (ORCA_AUTO_CONFIG_ENV_VAR, "PYTHONPATH")
            env_payload = {}
            for key in allowed_env_keys:
                value = normalize_text(self.env.get(key))
                if value:
                    env_payload[key] = value
            if not env_payload:
                env_payload = None
        return {
            "app": self.app,
            "argv": list(self.argv),
            "cwd": self.cwd or "",
            "env": env_payload,
            "restart_on_clean_exit": self.restart_on_clean_exit,
        }


def _selected_worker_apps(values: Sequence[str] | None) -> list[str]:
    selected = list(values or [])
    if not selected:
        return list(_DEFAULT_WORKER_APPS)

    ordered: list[str] = []
    seen: set[str] = set()
    for value in selected:
        text = normalize_text(value).lower()
        if not text or text in seen:
            continue
        if text not in _KNOWN_WORKER_APPS:
            raise ValueError(f"Unsupported worker app: {text}")
        seen.add(text)
        ordered.append(text)
    return ordered


def _engine_worker_tail_argv(*, app: str) -> list[str]:
    return ["--engine", app]


def worker_module_command(
    *,
    config_path: str,
    repo_root: str | None,
    module_name: str,
    tail_argv: list[str],
) -> tuple[list[str], str | None, dict[str, str] | None]:
    argv = [sys.executable, "-m", module_name, "--config", config_path, *tail_argv]
    if repo_root is None:
        return argv, None, None

    root_path = Path(repo_root).expanduser().resolve()
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    candidates = [str(root_path)]
    src_root = root_path / "src"
    if src_root.is_dir():
        candidates.insert(0, str(src_root))
    pythonpath = ":".join(candidates)
    env["PYTHONPATH"] = pythonpath if not existing else f"{pythonpath}:{existing}"
    return argv, str(root_path), env


def _engine_worker_spec(
    *,
    app: str,
    config_path: str,
) -> WorkerSpec:
    argv, cwd, env = worker_module_command(
        config_path=config_path,
        repo_root=_repo_root_for_subprocess(),
        module_name=_ENGINE_WORKER_MODULES[app],
        tail_argv=_engine_worker_tail_argv(app=app),
    )
    env_payload = dict(env) if isinstance(env, dict) else None
    return WorkerSpec(app=app, argv=tuple(argv), cwd=cwd, env=env_payload)


def _workflow_worker_spec(
    *,
    workflow_root: str,
    config_path: str | None,
    args: argparse.Namespace,
) -> WorkerSpec:
    argv = [
        sys.executable,
        "-m",
        ORCA_AUTO_WORKFLOW_WORKER_MODULE,
        "--workflow-root",
        str(Path(workflow_root).expanduser().resolve()),
    ]
    if normalize_text(config_path):
        argv.extend(["--orca_auto-config", str(Path(str(config_path)).expanduser().resolve())])
    if bool(getattr(args, "no_submit", False)):
        argv.append("--no-submit")
    if bool(getattr(args, "once", False)):
        argv.append("--once")
    if bool(getattr(args, "refresh_registry", False)):
        argv.append("--refresh-registry")
    if bool(getattr(args, "refresh_each_cycle", False)):
        argv.append("--refresh-each-cycle")

    max_cycles = int(getattr(args, "max_cycles", 0) or 0)
    if max_cycles > 0:
        argv.extend(["--max-cycles", str(max_cycles)])

    interval_seconds = float(getattr(args, "interval_seconds", 0.0) or 0.0)
    if interval_seconds > 0:
        argv.extend(["--interval-seconds", str(interval_seconds)])

    lock_timeout_seconds = float(getattr(args, "lock_timeout_seconds", 0.0) or 0.0)
    if lock_timeout_seconds > 0:
        argv.extend(["--lock-timeout-seconds", str(lock_timeout_seconds)])
    finite_worker = bool(getattr(args, "once", False)) or max_cycles > 0
    return WorkerSpec(
        app="workflow",
        argv=tuple(argv),
        restart_on_clean_exit=not finite_worker,
    )


def _worker_engine_apps(apps: Sequence[str], *, workflow_enabled: bool) -> list[str]:
    engine_apps = [app for app in apps if app in _ENGINE_APPS]
    if workflow_enabled:
        for app in _WORKFLOW_ENGINE_APPS:
            if app not in engine_apps:
                engine_apps.append(app)
    return engine_apps


def _validate_engine_worker_config(engine_apps: Sequence[str], config_path: str | None) -> None:
    if engine_apps and not normalize_text(config_path):
        raise ValueError(
            "Could not discover orca_auto.yaml for engine workers. Pass --orca_auto-config or set ORCA_AUTO_CONFIG."
        )


def _workflow_only_worker_flag_error(args: Any) -> str | None:
    if any(
        bool(getattr(args, attr, False))
        for attr in ("no_submit", "refresh_registry", "refresh_each_cycle")
    ):
        raise ValueError("workflow-only worker flags require --app workflow")
    numeric_flags = (
        ("max_cycles", int, "--max-cycles"),
        ("interval_seconds", float, "--interval-seconds"),
        ("lock_timeout_seconds", float, "--lock-timeout-seconds"),
    )
    for attr, caster, option in numeric_flags:
        if caster(getattr(args, attr, 0) or 0) > 0:
            return f"{option} requires --app workflow"
    return None


def _add_workflow_worker_spec(
    specs: list[WorkerSpec],
    *,
    apps: Sequence[str],
    workflow_root: str | None,
    config_path: str | None,
    args: argparse.Namespace,
) -> None:
    if "workflow" in apps and not workflow_root:
        raise ValueError("workflow worker requires runs_root in orca_auto.yaml")

    if "workflow" in apps and workflow_root:
        specs.append(
            _workflow_worker_spec(workflow_root=workflow_root, config_path=config_path, args=args)
        )
        return

    flag_error = _workflow_only_worker_flag_error(args)
    if flag_error:
        raise ValueError(flag_error)


def _build_worker_specs(args: Any) -> list[WorkerSpec]:
    explicit_apps = list(getattr(args, "app", None) or [])
    apps = _selected_worker_apps(explicit_apps)
    config_path = _discover_shared_config_path(_effective_shared_config_text(args))
    workflow_root = _workflow_root_for_args(args)
    workflow_enabled = "workflow" in apps
    engine_apps = _worker_engine_apps(apps, workflow_enabled=workflow_enabled)
    _validate_engine_worker_config(engine_apps, config_path)

    specs = [_engine_worker_spec(app=app, config_path=str(config_path)) for app in engine_apps]
    _add_workflow_worker_spec(
        specs,
        apps=apps,
        workflow_root=workflow_root,
        config_path=config_path,
        args=args,
    )
    return specs


@dataclass(frozen=True)
class _ExistingWorkerConflict:
    app: str
    pid: int
    allowed_root: str
    command: str


def _read_process_command(pid: int) -> tuple[str, ...]:
    cmdline_path = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = cmdline_path.read_bytes()
    except OSError:
        LOGGER.debug("failed to read process command for pid %s", pid, exc_info=True)
        return ()
    parts = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
    return tuple(parts)


def _format_command_argv(command_argv: Sequence[str]) -> str:
    if not command_argv:
        return "<unavailable>"
    return _quoted_command(command_argv)


def _quoted_command(command_argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command_argv)


def _detect_existing_orca_worker_conflict(
    specs: Sequence[WorkerSpec],
    *,
    args: argparse.Namespace,
) -> _ExistingWorkerConflict | None:
    if not any(spec.app == "orca" for spec in specs):
        return None

    config_path = _discover_shared_config_path(_effective_shared_config_text(args))
    if not normalize_text(config_path):
        return None

    try:
        from orca_auto.orca.config import load_config as _load_orca_config
        from orca_auto.orca.engine import read_worker_pid as _read_orca_worker_pid

        cfg = _load_orca_config(str(config_path))
    except Exception:  # noqa: BLE001
        LOGGER.debug("failed to inspect existing ORCA worker config", exc_info=True)
        return None

    allowed_root = Path(cfg.runtime.allowed_root).expanduser().resolve()
    existing_pid = _read_orca_worker_pid(allowed_root)
    if existing_pid is None:
        return None

    command_argv = _read_process_command(existing_pid)
    return _ExistingWorkerConflict(
        app="orca",
        pid=existing_pid,
        allowed_root=str(allowed_root),
        command=_format_command_argv(command_argv),
    )


def _emit_existing_orca_worker_conflict(
    conflict: _ExistingWorkerConflict,
    *,
    command_name: str,
) -> int:
    del command_name
    print(
        f"error: existing ORCA queue worker detected for allowed_root {conflict.allowed_root} "
        f"(pid={conflict.pid})."
    )
    # The recorded argv identifies the holder far better than a two-way label
    # ever did, and the refusal is the same either way.
    print(f"command: {conflict.command}")
    print("Stop the existing worker before starting another worker.")
    return 1


_WORKER_POLL_INTERVAL_SECONDS = 1.0
_WORKER_START_STAGGER_SECONDS = 2.0
_WORKER_STARTUP_FAILURE_WINDOW_SECONDS = 5.0
_WORKER_MAX_STARTUP_FAILURES = 2
_WORKER_RESTART_WINDOW_SECONDS = 300.0
_WORKER_MAX_RESTARTS_IN_WINDOW = 3


@dataclass
class _SupervisedWorker:
    spec: WorkerSpec
    process: subprocess.Popen[Any]
    started_at_monotonic: float
    startup_failure_count: int = 0
    restart_timestamps: list[float] = field(default_factory=list)


@dataclass
class _SupervisorShutdown:
    requested: bool = False


def _terminate_process(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        LOGGER.debug("failed to terminate supervised worker process", exc_info=True)
        return

    deadline = time.monotonic() + 10.0
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if proc.poll() is not None:
        return

    try:
        proc.kill()
    except OSError:
        LOGGER.debug("failed to kill supervised worker process", exc_info=True)
        return

    deadline = time.monotonic() + 5.0
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)


def _spawn_supervised_worker(spec: WorkerSpec, *, restart: bool = False) -> _SupervisedWorker:
    command_text = _quoted_command(spec.argv)
    action = "restarting" if restart else "starting"
    print(f"{action} worker[{spec.app}]: {command_text}")
    return _SupervisedWorker(
        spec=spec,
        # A worker must not share the supervisor's process group. Otherwise a
        # worker-side group signal can terminate the supervisor and all of its
        # siblings, causing systemd to restart the entire worker set at once.
        process=subprocess.Popen(
            spec.argv,
            cwd=spec.cwd,
            env=spec.env,
            start_new_session=True,
        ),
        started_at_monotonic=time.monotonic(),
    )


def _install_supervisor_signal_handlers(shutdown: _SupervisorShutdown) -> dict[Any, Any]:
    def _request_shutdown(signum: int, frame: Any) -> None:
        del signum, frame
        shutdown.requested = True

    previous_handlers: dict[Any, Any] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _request_shutdown)
        except Exception:  # noqa: BLE001
            LOGGER.debug("failed to install worker supervisor signal handler", exc_info=True)
            continue
    return previous_handlers


def _restore_signal_handlers(previous_handlers: dict[Any, Any]) -> None:
    for sig, handler in previous_handlers.items():
        try:
            signal.signal(sig, handler)
        except Exception:  # noqa: BLE001
            LOGGER.debug("failed to restore worker supervisor signal handler", exc_info=True)
            continue


def _reset_stable_startup_failure_count(
    managed: _SupervisedWorker,
    current_time: float,
) -> None:
    if (
        managed.startup_failure_count > 0
        and current_time - managed.started_at_monotonic >= _WORKER_STARTUP_FAILURE_WINDOW_SECONDS
    ):
        managed.startup_failure_count = 0


def _restart_or_stop_worker(
    processes: list[_SupervisedWorker],
    *,
    index: int,
    managed: _SupervisedWorker,
    returncode: int,
    current_time: float,
) -> int | None:
    spec = managed.spec
    quick_startup_failure = (
        returncode != 0
        and current_time - managed.started_at_monotonic < _WORKER_STARTUP_FAILURE_WINDOW_SECONDS
    )
    if quick_startup_failure:
        managed.startup_failure_count += 1
        if managed.startup_failure_count >= _WORKER_MAX_STARTUP_FAILURES:
            print(
                f"worker[{spec.app}] failed repeatedly during startup; "
                "stopping supervisor to avoid a restart loop."
            )
            return returncode if returncode > 0 else 1
    else:
        managed.startup_failure_count = 0

    if returncode == 0 and not spec.restart_on_clean_exit:
        print(f"worker[{spec.app}] completed cleanly; stopping supervisor.")
        return 0

    restart_cutoff = current_time - _WORKER_RESTART_WINDOW_SECONDS
    managed.restart_timestamps[:] = [
        timestamp for timestamp in managed.restart_timestamps if timestamp >= restart_cutoff
    ]
    managed.restart_timestamps.append(current_time)
    if len(managed.restart_timestamps) >= _WORKER_MAX_RESTARTS_IN_WINDOW:
        print(
            f"worker[{spec.app}] exited repeatedly within "
            f"{int(_WORKER_RESTART_WINDOW_SECONDS)} seconds; "
            "stopping supervisor to avoid a restart loop."
        )
        return returncode if returncode > 0 else 1

    restarted = _spawn_supervised_worker(spec, restart=True)
    restarted.startup_failure_count = managed.startup_failure_count
    restarted.restart_timestamps = list(managed.restart_timestamps)
    processes[index] = restarted
    return None


def _poll_supervised_workers(
    processes: list[_SupervisedWorker],
    shutdown: _SupervisorShutdown,
) -> int | None:
    current_time = time.monotonic()
    for index, managed in enumerate(processes):
        returncode = managed.process.poll()
        if returncode is None:
            _reset_stable_startup_failure_count(managed, current_time)
            continue

        print(f"worker[{managed.spec.app}] exited with code {returncode}")
        if shutdown.requested:
            continue

        exit_code = _restart_or_stop_worker(
            processes,
            index=index,
            managed=managed,
            returncode=returncode,
            current_time=current_time,
        )
        if exit_code is not None:
            shutdown.requested = True
            return exit_code
    return None


def _supervise_worker_processes(
    processes: list[_SupervisedWorker],
    shutdown: _SupervisorShutdown,
) -> int:
    exit_code = 0
    while True:
        failure_exit_code = _poll_supervised_workers(processes, shutdown)
        if failure_exit_code is not None:
            exit_code = failure_exit_code
        if shutdown.requested:
            return exit_code
        time.sleep(_WORKER_POLL_INTERVAL_SECONDS)


def _terminate_supervised_workers(processes: Sequence[_SupervisedWorker]) -> None:
    for managed in processes:
        _terminate_process(managed.process)


def _run_worker_supervisor(
    specs: Sequence[WorkerSpec],
    *,
    startup_stagger_seconds: float = _WORKER_START_STAGGER_SECONDS,
) -> int:
    if not specs:
        emit_error("no workers selected")
        return 1

    processes: list[_SupervisedWorker] = []
    shutdown = _SupervisorShutdown()
    previous_handlers = _install_supervisor_signal_handlers(shutdown)
    try:
        for index, spec in enumerate(specs):
            processes.append(_spawn_supervised_worker(spec))
            if index + 1 < len(specs) and startup_stagger_seconds > 0:
                time.sleep(startup_stagger_seconds)
                if shutdown.requested:
                    break
        return _supervise_worker_processes(processes, shutdown)
    finally:
        _terminate_supervised_workers(processes)
        _restore_signal_handlers(previous_handlers)


def _emit_supervisor_specs_json(*, key: str, specs: Sequence[WorkerSpec]) -> int:
    print(json.dumps({key: [spec.to_dict() for spec in specs]}, ensure_ascii=True, indent=2))
    return 0


def cmd_queue_worker(args: Any) -> int:
    try:
        specs = _build_worker_specs(args)
    except ValueError as exc:
        emit_error(exc)
        return 1

    if bool(getattr(args, "json", False)):
        return _emit_supervisor_specs_json(key="workers", specs=specs)

    conflict = _detect_existing_orca_worker_conflict(specs, args=args)
    if conflict is not None:
        return _emit_existing_orca_worker_conflict(conflict, command_name="queue worker")

    return _run_worker_supervisor(specs)
