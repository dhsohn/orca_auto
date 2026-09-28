"""Supervise the one queue worker process: start it, restart it within a cap, stop it."""

from __future__ import annotations

import logging
import shlex
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from orca_auto.core.config.schema import SchedulerConfig
from orca_auto.core.queue.processes import KILL_TIMEOUT_SECONDS, worker_shutdown_budget_seconds

LOGGER = logging.getLogger(__name__)

# The name the supervisor's lines and ``queue worker --json`` give the worker.
WORKER_APP = "orca"

_WORKER_POLL_INTERVAL_SECONDS = 1.0
_WORKER_STARTUP_FAILURE_WINDOW_SECONDS = 5.0
_WORKER_MAX_STARTUP_FAILURES = 2
_WORKER_RESTART_WINDOW_SECONDS = 300.0
_WORKER_MAX_RESTARTS_IN_WINDOW = 3


def worker_stop_budget_seconds(max_active_simulations: int | None) -> float:
    """How long the supervisor waits after SIGTERM before it kills the worker.

    The worker's own worst-case shutdown (stop every child, requeue each row),
    derived from the constants the worker stops children with. ``None`` (a
    config that did not load) gets the default concurrency's budget; the worker
    then fails at startup on the same config.
    """
    if max_active_simulations is None:
        max_active_simulations = SchedulerConfig.max_active_simulations
    return worker_shutdown_budget_seconds(max_active_simulations)


def quoted_command(command_argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command_argv)


@dataclass
class _SupervisorShutdown:
    requested: bool = False


def _terminate_process(proc: subprocess.Popen[Any], *, stop_timeout_seconds: float) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        LOGGER.debug("failed to terminate supervised worker process", exc_info=True)
        return

    deadline = time.monotonic() + max(0.0, float(stop_timeout_seconds))
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if proc.poll() is not None:
        return

    try:
        proc.kill()
    except OSError:
        LOGGER.debug("failed to kill supervised worker process", exc_info=True)
        return

    deadline = time.monotonic() + KILL_TIMEOUT_SECONDS
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)


def _spawn_worker(argv: Sequence[str], *, restart: bool) -> subprocess.Popen[Any]:
    action = "restarting" if restart else "starting"
    print(f"{action} worker[{WORKER_APP}]: {quoted_command(argv)}")
    # The worker must not share the supervisor's process group. Otherwise a
    # worker-side group signal can terminate the supervisor as well, causing
    # systemd to restart both at once.
    return subprocess.Popen(list(argv), start_new_session=True)


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


def run_worker_supervisor(argv: Sequence[str], *, stop_timeout_seconds: float) -> int:
    """Run the worker until SIGINT/SIGTERM, restarting it after every exit.

    Two consecutive failing exits within 5 s of a start, or three exits within
    300 s, stop the supervisor instead of looping. On the way out the worker
    gets SIGTERM and ``stop_timeout_seconds`` to finish before it is killed.
    """
    shutdown = _SupervisorShutdown()
    previous_handlers = _install_supervisor_signal_handlers(shutdown)
    process: subprocess.Popen[Any] | None = None
    try:
        process = _spawn_worker(argv, restart=False)
        started_at = time.monotonic()
        startup_failures = 0
        restart_times: list[float] = []
        while True:
            current_time = time.monotonic()
            returncode = process.poll()
            if returncode is not None:
                print(f"worker[{WORKER_APP}] exited with code {returncode}")
                if not shutdown.requested:
                    if (
                        returncode != 0
                        and current_time - started_at < _WORKER_STARTUP_FAILURE_WINDOW_SECONDS
                    ):
                        startup_failures += 1
                        if startup_failures >= _WORKER_MAX_STARTUP_FAILURES:
                            print(
                                f"worker[{WORKER_APP}] failed repeatedly during startup; "
                                "stopping supervisor to avoid a restart loop."
                            )
                            return returncode if returncode > 0 else 1
                    else:
                        startup_failures = 0
                    restart_cutoff = current_time - _WORKER_RESTART_WINDOW_SECONDS
                    restart_times = [stamp for stamp in restart_times if stamp >= restart_cutoff]
                    restart_times.append(current_time)
                    if len(restart_times) >= _WORKER_MAX_RESTARTS_IN_WINDOW:
                        print(
                            f"worker[{WORKER_APP}] exited repeatedly within "
                            f"{int(_WORKER_RESTART_WINDOW_SECONDS)} seconds; "
                            "stopping supervisor to avoid a restart loop."
                        )
                        return returncode if returncode > 0 else 1
                    process = _spawn_worker(argv, restart=True)
                    started_at = time.monotonic()
            if shutdown.requested:
                return 0
            time.sleep(_WORKER_POLL_INTERVAL_SECONDS)
    finally:
        if process is not None:
            _terminate_process(process, stop_timeout_seconds=stop_timeout_seconds)
        _restore_signal_handlers(previous_handlers)
