from __future__ import annotations

import json
import logging
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from orca_auto.cli_errors import emit_error
from orca_auto.cli_worker_conflicts import (
    _detect_existing_orca_worker_conflict,
    _emit_existing_orca_worker_conflict,
    _quoted_command,
)
from orca_auto.cli_worker_specs import (
    WorkerSpec,
    _build_worker_specs,
)

_WORKER_POLL_INTERVAL_SECONDS = 1.0
_WORKER_START_STAGGER_SECONDS = 2.0
_WORKER_STARTUP_FAILURE_WINDOW_SECONDS = 5.0
_WORKER_MAX_STARTUP_FAILURES = 2
_WORKER_RESTART_WINDOW_SECONDS = 300.0
_WORKER_MAX_RESTARTS_IN_WINDOW = 3
LOGGER = logging.getLogger(__name__)


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
