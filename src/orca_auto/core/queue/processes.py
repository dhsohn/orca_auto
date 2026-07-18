from __future__ import annotations

import errno
import json
import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from orca_auto.core.utils import process as process_utils
from orca_auto.core.utils.persistence import atomic_write_text, now_utc_iso

LOGGER = logging.getLogger(__name__)


class ManagedProcess(Protocol):
    pid: int

    def kill(self) -> None: ...
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True)
class ProcessGroupTerminationDeps:
    killpg: Callable[[int, int], None] | None = None
    process_group_exists: Callable[[int], bool] | None = None
    pid_exists: Callable[[int], bool] | None = None
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    group_poll_interval_seconds: float = 0.1
    sigterm: int = signal.SIGTERM
    sigkill: int = signal.SIGKILL
    logger: logging.Logger = LOGGER


@dataclass(frozen=True)
class ShutdownSignalDeps:
    signal_fn: Callable[[int, Callable[[int, object], None]], Any] = signal.signal
    sigterm: int = signal.SIGTERM
    sigint: int = signal.SIGINT
    logger: logging.Logger = LOGGER


def process_group_exists(
    pgid: int,
    *,
    killpg_fn: Callable[[int, int], None] | None = None,
) -> bool:
    """Return whether a POSIX process group exists, failing closed on probe errors."""
    active_killpg = killpg_fn or getattr(os, "killpg", None)
    if active_killpg is None:
        # There is no POSIX process-group concept to verify on this platform.
        return False
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


def terminate_process_group(
    proc: ManagedProcess,
    *,
    graceful_timeout: float = 10,
    kill_timeout: float = 5,
    killpg_fn: Callable[[int, int], None] | None = None,
    sigterm: int | None = None,
    sigkill: int | None = None,
    deps: ProcessGroupTerminationDeps | None = None,
) -> bool:
    active_deps = deps or ProcessGroupTerminationDeps()
    active_killpg = killpg_fn
    if active_killpg is None:
        active_killpg = active_deps.killpg
    if active_killpg is None:
        active_killpg = getattr(os, "killpg", None)
    active_sigterm = active_deps.sigterm if sigterm is None else sigterm
    active_sigkill = active_deps.sigkill if sigkill is None else sigkill
    logger = active_deps.logger
    group_exists = active_deps.process_group_exists
    if group_exists is None:

        def group_exists(pgid: int) -> bool:
            return process_group_exists(pgid, killpg_fn=active_killpg)

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
        if active_killpg is None:
            raise ProcessLookupError
        active_killpg(pid, active_sigterm)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            logger.debug("failed to terminate process after group signal failed", exc_info=True)

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
        if active_killpg is None:
            raise ProcessLookupError
        active_killpg(pid, active_sigkill)
    except (ProcessLookupError, PermissionError, OSError):
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


def install_shutdown_signal_handlers(
    request_shutdown: Callable[[], None],
    *,
    deps: ShutdownSignalDeps | None = None,
) -> None:
    active_deps = deps or ShutdownSignalDeps()

    def _handle_signal(_signum: int, _frame: object) -> None:
        request_shutdown()

    try:
        active_deps.signal_fn(active_deps.sigterm, _handle_signal)
        active_deps.signal_fn(active_deps.sigint, _handle_signal)
    except ValueError:
        active_deps.logger.debug(
            "shutdown signal handlers can only be installed from the main thread"
        )


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _process_start_ticks(pid: int) -> int | None:
    return process_utils.process_start_ticks(pid, proc_root=Path("/proc"))


def current_worker_pid_payload() -> dict[str, int | str]:
    return process_utils.current_pid_payload(
        now_fn=now_utc_iso,
        process_start_ticks_fn=_process_start_ticks,
        pid_fn=os.getpid,
        boot_id_fn=lambda: process_utils.linux_boot_id(proc_root=Path("/proc")),
    )


def worker_pid_file_path(allowed_root: Path | str, file_name: str = "queue_worker.pid") -> Path:
    return Path(allowed_root).expanduser().resolve() / file_name


def write_worker_pid_file(allowed_root: Path | str, file_name: str = "queue_worker.pid") -> None:
    payload = current_worker_pid_payload()
    atomic_write_text(
        worker_pid_file_path(allowed_root, file_name),
        json.dumps(payload, ensure_ascii=True) + "\n",
    )


def remove_worker_pid_file(allowed_root: Path | str, file_name: str = "queue_worker.pid") -> None:
    process_utils.remove_file_silent(worker_pid_file_path(allowed_root, file_name))


def read_worker_pid_file(
    allowed_root: Path | str, file_name: str = "queue_worker.pid"
) -> int | None:
    return read_live_pid_file(worker_pid_file_path(allowed_root, file_name))


def read_live_pid_file(pid_path: Path) -> int | None:
    return process_utils.read_live_pid_file(
        pid_path,
        is_process_alive_fn=pid_is_alive,
        process_start_ticks_fn=_process_start_ticks,
        boot_id_fn=lambda: process_utils.linux_boot_id(proc_root=Path("/proc")),
        remove_file_fn=process_utils.remove_file_silent,
    )


__all__ = [
    "ManagedProcess",
    "ProcessGroupTerminationDeps",
    "ShutdownSignalDeps",
    "current_worker_pid_payload",
    "install_shutdown_signal_handlers",
    "managed_process_group_has_exited",
    "pid_is_alive",
    "process_group_exists",
    "read_live_pid_file",
    "read_worker_pid_file",
    "remove_worker_pid_file",
    "terminate_process_group",
    "worker_pid_file_path",
    "write_worker_pid_file",
]
