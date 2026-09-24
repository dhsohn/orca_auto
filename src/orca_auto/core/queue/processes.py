from __future__ import annotations

import errno
import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

LOGGER = logging.getLogger(__name__)

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


class ManagedProcess(Protocol):
    pid: int

    def kill(self) -> None: ...
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True)
class ProcessGroupTerminationDeps:
    process_group_exists: Callable[[int], bool] | None = None
    pid_exists: Callable[[int], bool] | None = None
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    group_poll_interval_seconds: float = 0.1
    sigterm: int = signal.SIGTERM
    sigkill: int = signal.SIGKILL
    logger: logging.Logger = LOGGER


def process_group_exists(
    pgid: int,
    *,
    killpg_fn: Callable[[int, int], None] | None = None,
) -> bool:
    """Return whether a POSIX process group exists, failing closed on probe errors."""
    active_killpg = os.killpg if killpg_fn is None else killpg_fn
    try:
        active_killpg(int(pgid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _pid_exists(pid: int) -> bool:
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


def _group_signal_is_safe(
    proc: ManagedProcess,
    *,
    pid_exists_fn: Callable[[int], bool],
) -> bool | None:
    """Return True when owned/leaderless, False on proven reuse, None if unknown."""
    try:
        if proc.poll() is None:
            return True
    except Exception:  # noqa: BLE001
        return None
    try:
        return not pid_exists_fn(proc.pid)
    except Exception:  # noqa: BLE001
        return None


def managed_process_group_has_exited(
    proc: ManagedProcess,
    *,
    process_group_exists_fn: Callable[[int], bool] | None = None,
    killpg_fn: Callable[[int, int], None] | None = None,
    pid_exists_fn: Callable[[int], bool] = _pid_exists,
) -> bool:
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
        if pid_exists_fn(pid):
            return True
    except Exception:  # noqa: BLE001
        return False
    group_exists = process_group_exists_fn or (
        lambda pgid: process_group_exists(pgid, killpg_fn=killpg_fn)
    )
    try:
        return not group_exists(pid)
    except Exception:  # noqa: BLE001
        return False


def _wait_for_managed_process_group_exit(
    proc: ManagedProcess,
    *,
    timeout_seconds: float,
    process_group_exists_fn: Callable[[int], bool],
    deps: ProcessGroupTerminationDeps,
) -> bool:
    deadline = deps.monotonic() + max(0.0, float(timeout_seconds))
    interval = max(0.01, float(deps.group_poll_interval_seconds))
    leader_exited = False
    while True:
        if leader_exited:
            pid_exists_fn = deps.pid_exists or _pid_exists
            try:
                if pid_exists_fn(proc.pid):
                    return True
                if not process_group_exists_fn(proc.pid):
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
        remaining = deadline - deps.monotonic()
        if remaining <= 0:
            return False
        try:
            if not leader_exited:
                proc.wait(timeout=remaining)
                leader_exited = True
                continue
        except subprocess.TimeoutExpired:
            return managed_process_group_has_exited(
                proc,
                process_group_exists_fn=process_group_exists_fn,
                pid_exists_fn=deps.pid_exists or _pid_exists,
            )
        except Exception:  # noqa: BLE001
            deps.logger.debug("failed while waiting for process leader", exc_info=True)
        deps.sleep(min(interval, remaining))


def _group_exists_fn(
    deps: ProcessGroupTerminationDeps, killpg_fn: Callable[[int, int], None]
) -> Callable[[int], bool]:
    if deps.process_group_exists is not None:
        return deps.process_group_exists
    return lambda pgid: process_group_exists(pgid, killpg_fn=killpg_fn)


def request_process_group_stop(
    proc: ManagedProcess,
    *,
    killpg_fn: Callable[[int, int], None] | None = None,
    sigterm: int | None = None,
    deps: ProcessGroupTerminationDeps | None = None,
) -> bool | None:
    """Send SIGTERM to the group without waiting.

    True: the group has already exited. None: the signal was sent (or the
    leader was terminated directly). False: the group could not be signalled
    safely. ``terminate_process_group`` waits and escalates after this step;
    a shutdown sweep uses it first so every child stops concurrently.
    """
    active_deps = deps or ProcessGroupTerminationDeps()
    active_killpg = os.killpg if killpg_fn is None else killpg_fn
    active_sigterm = active_deps.sigterm if sigterm is None else sigterm
    logger = active_deps.logger
    group_exists = _group_exists_fn(active_deps, active_killpg)

    if managed_process_group_has_exited(
        proc,
        process_group_exists_fn=group_exists,
        pid_exists_fn=active_deps.pid_exists or _pid_exists,
    ):
        return True

    pid = getattr(proc, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        logger.warning(
            "Refusing to signal process group with invalid pgid=%r",
            pid,
        )
        return False

    signal_safety = _group_signal_is_safe(
        proc,
        pid_exists_fn=active_deps.pid_exists or _pid_exists,
    )
    if signal_safety is False:
        return True
    if signal_safety is None:
        logger.warning(
            "Cannot verify reaped process-group identity; refusing to signal pgid=%s",
            getattr(proc, "pid", None),
        )
        return False

    try:
        active_killpg(pid, active_sigterm)
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
    killpg_fn: Callable[[int, int], None] | None = None,
    sigterm: int | None = None,
    sigkill: int | None = None,
    deps: ProcessGroupTerminationDeps | None = None,
) -> bool:
    active_deps = deps or ProcessGroupTerminationDeps()
    active_killpg = os.killpg if killpg_fn is None else killpg_fn
    active_sigkill = active_deps.sigkill if sigkill is None else sigkill
    logger = active_deps.logger
    group_exists = _group_exists_fn(active_deps, active_killpg)

    requested = request_process_group_stop(
        proc, killpg_fn=killpg_fn, sigterm=sigterm, deps=active_deps
    )
    if requested is not None:
        return requested
    pid = proc.pid

    if _wait_for_managed_process_group_exit(
        proc,
        timeout_seconds=graceful_timeout,
        process_group_exists_fn=group_exists,
        deps=active_deps,
    ):
        return True

    signal_safety = _group_signal_is_safe(
        proc,
        pid_exists_fn=active_deps.pid_exists or _pid_exists,
    )
    if signal_safety is False:
        return True
    if signal_safety is None:
        logger.warning(
            "Cannot verify reaped process-group identity before SIGKILL; pgid=%s",
            getattr(proc, "pid", None),
        )
        return False

    try:
        active_killpg(pid, active_sigkill)
    except OSError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            logger.debug("failed to kill process after group kill failed", exc_info=True)
    terminated = _wait_for_managed_process_group_exit(
        proc,
        timeout_seconds=kill_timeout,
        process_group_exists_fn=group_exists,
        deps=active_deps,
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
    "ProcessGroupTerminationDeps",
    "install_shutdown_signal_handlers",
    "managed_process_group_has_exited",
    "process_group_exists",
    "request_process_group_stop",
    "terminate_process_group",
    "worker_shutdown_budget_seconds",
]
