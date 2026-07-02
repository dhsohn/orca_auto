from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
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
    sigterm: int = signal.SIGTERM
    sigkill: int = signal.SIGKILL
    logger: logging.Logger = LOGGER


@dataclass(frozen=True)
class ShutdownSignalDeps:
    signal_fn: Callable[[int, Callable[[int, object], None]], Any] = signal.signal
    sigterm: int = signal.SIGTERM
    sigint: int = signal.SIGINT
    logger: logging.Logger = LOGGER


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
    if proc.poll() is not None:
        return True
    active_deps = deps or ProcessGroupTerminationDeps()
    active_killpg = killpg_fn
    if active_killpg is None:
        active_killpg = active_deps.killpg
    if active_killpg is None:
        active_killpg = os.killpg
    active_sigterm = active_deps.sigterm if sigterm is None else sigterm
    active_sigkill = active_deps.sigkill if sigkill is None else sigkill
    logger = active_deps.logger

    try:
        active_killpg(proc.pid, active_sigterm)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            logger.debug("failed to terminate process after group signal failed", exc_info=True)

    try:
        proc.wait(timeout=graceful_timeout)
        return True
    except subprocess.TimeoutExpired:
        if proc.poll() is not None:
            return True
        try:
            active_killpg(proc.pid, active_sigkill)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                logger.debug("failed to kill process after group kill failed", exc_info=True)
        try:
            proc.wait(timeout=kill_timeout)
            return True
        except subprocess.TimeoutExpired:
            if proc.poll() is not None:
                return True
            logger.debug("process did not exit after kill timeout: pid=%s", proc.pid)
            return False


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
        remove_file_fn=process_utils.remove_file_silent,
    )


__all__ = [
    "ManagedProcess",
    "ProcessGroupTerminationDeps",
    "ShutdownSignalDeps",
    "current_worker_pid_payload",
    "install_shutdown_signal_handlers",
    "pid_is_alive",
    "read_live_pid_file",
    "read_worker_pid_file",
    "remove_worker_pid_file",
    "terminate_process_group",
    "worker_pid_file_path",
    "write_worker_pid_file",
]
