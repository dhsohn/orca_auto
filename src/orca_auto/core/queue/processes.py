"""Worker child processes: spawning each in its own session and stopping its group."""

from __future__ import annotations

import errno
import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from ..confined_io import open_confined_log
from ..utils import process as process_utils

LOGGER = logging.getLogger(__name__)
_GROUP_POLL_INTERVAL_SECONDS = 0.1

# One child's stop: SIGTERM, then SIGKILL after the graceful wait, then the
# kill wait. The worker stops children in parallel first, then each job in turn, so its whole shutdown
# needs at most this per child; the supervisor and the systemd unit derive
# their stop budgets from the same numbers through
# ``worker_shutdown_budget_seconds``.
GRACEFUL_TIMEOUT_SECONDS = 10.0
KILL_TIMEOUT_SECONDS = 5.0
# Covers the per-job requeue or replay that follows each child's exit.
SHUTDOWN_MARGIN_SECONDS = 15.0
# SIGTERM does not interrupt a sleeping poll (PEP 475): the worker may finish
# its current poll sleep (up to 5 s) and the supervisor polls the worker once a
# second before it notices the exit, so both latencies are part of the budget.
SHUTDOWN_POLL_LATENCY_SECONDS = 6.0


def worker_shutdown_budget_seconds(max_concurrent: int) -> float:
    """Worst-case seconds a worker needs to stop every child and requeue each row."""
    return (
        (GRACEFUL_TIMEOUT_SECONDS + KILL_TIMEOUT_SECONDS) * max(1, int(max_concurrent))
        + SHUTDOWN_POLL_LATENCY_SECONDS
        + SHUTDOWN_MARGIN_SECONDS
    )


def start_background_process(
    command: Sequence[str],
    *,
    log_path: str | Path | None = None,
) -> subprocess.Popen[str]:
    if log_path is None:
        return subprocess.Popen(
            list(command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )

    resolved_log_path = Path(log_path).expanduser()
    resolved_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open_confined_log(
        resolved_log_path.parent,
        resolved_log_path,
        label="background worker log",
        append=True,
    ) as log_handle:
        return subprocess.Popen(
            list(command),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )


class ManagedProcess(Protocol):
    pid: int

    def kill(self) -> None: ...
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


def _pid_exists(pid: int) -> bool:
    """:func:`process_utils.is_process_alive`, except that an unexpected errno raises.

    Every caller treats the raise as unknown and fails closed.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise
    return True


def _group_signal_is_safe(proc: ManagedProcess) -> bool | None:
    """Return True when owned/leaderless, False on proven reuse, None if unknown."""
    try:
        if proc.poll() is None:
            return True
    except Exception:  # noqa: BLE001
        return None
    try:
        return not _pid_exists(proc.pid)
    except Exception:  # noqa: BLE001
        return None


def managed_process_group_has_exited(proc: ManagedProcess) -> bool:
    """True only after the leader and every member of its process group are gone."""
    try:
        if proc.poll() is None:
            return False
    except Exception:  # noqa: BLE001
        return False
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return True
    # Popen.poll() reaps its child. Linux cannot reuse that numeric PID while
    # the original PGID still exists; therefore a currently live process with
    # the same PID proves the observed group belongs to a later session.
    try:
        if _pid_exists(pid):
            return True
    except Exception:  # noqa: BLE001
        return False
    try:
        return not process_utils.process_group_exists(pid)
    except Exception:  # noqa: BLE001
        return False


class ProcessCleanupError(RuntimeError):
    """Raised when a live engine process cannot be confirmed terminated."""


def retain_process_ownership_until_exit(
    process: Any,
    *,
    terminate_process: Callable[[Any], bool],
    sleep: Callable[[float], None] = time.sleep,
    retry_attempts: int = 2,
    poll_interval_seconds: float = 1.0,
) -> None:
    """Do not let the owning worker exit while its engine process is live.

    Termination is retried a bounded number of times.  If it still cannot be
    confirmed, the owner remains alive and only polls for natural/external
    process exit; this keeps the admission slot fail-closed.
    """
    interval = max(0.0, float(poll_interval_seconds))
    for _attempt in range(max(0, int(retry_attempts))):
        if managed_process_group_has_exited(process):
            return
        sleep(interval)
        try:
            terminate_process(process)
        except Exception:  # noqa: BLE001
            pass
        if managed_process_group_has_exited(process):
            return

    while not managed_process_group_has_exited(process):
        sleep(interval)


def _wait_for_managed_process_group_exit(
    proc: ManagedProcess,
    *,
    timeout_seconds: float,
    logger: logging.Logger,
) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    leader_exited = False
    while True:
        if leader_exited:
            try:
                if _pid_exists(proc.pid):
                    return True
                if not process_utils.process_group_exists(proc.pid):
                    return True
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                leader_exited = proc.poll() is not None
            except Exception:  # noqa: BLE001
                leader_exited = False
            if leader_exited:
                continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            if not leader_exited:
                proc.wait(timeout=remaining)
                leader_exited = True
                continue
        except subprocess.TimeoutExpired:
            return managed_process_group_has_exited(proc)
        except Exception:  # noqa: BLE001
            logger.debug("failed while waiting for process leader", exc_info=True)
        time.sleep(min(_GROUP_POLL_INTERVAL_SECONDS, remaining))


def request_process_group_stop(
    proc: ManagedProcess, *, logger: logging.Logger = LOGGER
) -> bool | None:
    """Send SIGTERM to the group without waiting.

    True: the group has already exited. None: the signal was sent (or the
    leader was terminated directly). False: the group could not be signalled
    safely. ``terminate_process_group`` waits and escalates after this step;
    a shutdown sweep uses it first so every child stops concurrently.
    """
    if managed_process_group_has_exited(proc):
        return True

    pid = getattr(proc, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        logger.warning(
            "Refusing to signal process group with invalid pgid=%r",
            pid,
        )
        return False

    signal_safety = _group_signal_is_safe(proc)
    if signal_safety is False:
        return True
    if signal_safety is None:
        logger.warning(
            "Cannot verify reaped process-group identity; refusing to signal pgid=%s",
            getattr(proc, "pid", None),
        )
        return False

    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            logger.debug("failed to terminate process after group signal failed", exc_info=True)
    return None


def terminate_process_group(
    proc: ManagedProcess,
    *,
    graceful_timeout: float = GRACEFUL_TIMEOUT_SECONDS,
    kill_timeout: float = KILL_TIMEOUT_SECONDS,
    logger: logging.Logger = LOGGER,
) -> bool:
    requested = request_process_group_stop(proc, logger=logger)
    if requested is not None:
        return requested
    pid = proc.pid

    if _wait_for_managed_process_group_exit(proc, timeout_seconds=graceful_timeout, logger=logger):
        return True

    signal_safety = _group_signal_is_safe(proc)
    if signal_safety is False:
        return True
    if signal_safety is None:
        logger.warning(
            "Cannot verify reaped process-group identity before SIGKILL; pgid=%s",
            getattr(proc, "pid", None),
        )
        return False

    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            logger.debug("failed to kill process after group kill failed", exc_info=True)
    terminated = _wait_for_managed_process_group_exit(
        proc, timeout_seconds=kill_timeout, logger=logger
    )
    if not terminated:
        logger.debug("process group did not exit after kill timeout: pgid=%s", pid)
    return terminated


def install_shutdown_signal_handlers(request_shutdown: Callable[[], None]) -> None:
    def _handle_signal(_signum: int, _frame: object) -> None:
        request_shutdown()

    # signal.signal is looked up at call time so tests can patch the signal
    # module after import and still suppress real handler installation.
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
    except ValueError:
        LOGGER.debug("shutdown signal handlers can only be installed from the main thread")


__all__ = [
    "GRACEFUL_TIMEOUT_SECONDS",
    "KILL_TIMEOUT_SECONDS",
    "SHUTDOWN_MARGIN_SECONDS",
    "SHUTDOWN_POLL_LATENCY_SECONDS",
    "ManagedProcess",
    "ProcessCleanupError",
    "install_shutdown_signal_handlers",
    "managed_process_group_has_exited",
    "request_process_group_stop",
    "retain_process_ownership_until_exit",
    "start_background_process",
    "terminate_process_group",
    "worker_shutdown_budget_seconds",
]
