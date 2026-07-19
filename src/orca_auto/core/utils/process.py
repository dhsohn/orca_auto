from __future__ import annotations

import errno
import json
import os
import signal
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PIDFD_SIGNAL_PROCESS_GROUP = 1 << 2


class StableProcessSignalError(RuntimeError):
    """Raised when a process cannot be signalled through a stable pidfd target."""


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def process_start_ticks(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    if pid <= 0:
        return None
    stat_path = proc_root / str(pid) / "stat"
    try:
        text = stat_path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    if not text:
        return None
    right_paren = text.rfind(")")
    if right_paren < 0:
        return None
    fields_after_comm = text[right_paren + 2 :].split()
    if len(fields_after_comm) <= 19:
        return None
    try:
        value = int(fields_after_comm[19])
    except ValueError:
        return None
    return value if value > 0 else None


def linux_boot_id(*, proc_root: Path = Path("/proc")) -> str | None:
    """Return the Linux boot identifier used to scope process start ticks."""
    try:
        value = (proc_root / "sys/kernel/random/boot_id").read_text(encoding="utf-8")
    except OSError:
        return None
    normalized = value.strip()
    return normalized or None


def _open_proc_pid_directory(pid: int) -> int:
    flags = os.O_RDONLY
    flags |= os.O_DIRECTORY
    flags |= os.O_CLOEXEC
    return os.open(f"/proc/{pid}", flags)


def _read_proc_identity_from_directory_fd(directory_fd: int) -> tuple[int, int, int]:
    flags = os.O_RDONLY
    flags |= os.O_CLOEXEC
    stat_fd = os.open("stat", flags, dir_fd=directory_fd)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(stat_fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(stat_fd)
    text = b"".join(chunks).decode("utf-8", errors="strict").strip()
    left_paren = text.find("(")
    right_paren = text.rfind(")")
    if left_paren <= 0 or right_paren <= left_paren:
        raise ValueError("Invalid /proc process stat payload")
    fields_after_comm = text[right_paren + 1 :].strip().split()
    if len(fields_after_comm) <= 19:
        raise ValueError("Incomplete /proc process stat payload")
    pid = int(text[:left_paren].strip())
    process_group_id = int(fields_after_comm[2])
    start_ticks = int(fields_after_comm[19])
    if pid <= 0 or process_group_id <= 0 or start_ticks <= 0:
        raise ValueError("Invalid /proc process identity")
    return pid, process_group_id, start_ticks


def _pidfd_send_signal(pidfd: int, signum: int, flags: int) -> None:
    sender = getattr(signal, "pidfd_send_signal", None)
    if sender is None:
        raise OSError(errno.ENOSYS, "pidfd_send_signal is unavailable")
    sender(pidfd, signum, None, flags)


@dataclass(frozen=True)
class StableProcessSignalDeps:
    open_process: Callable[[int], int] = _open_proc_pid_directory
    read_identity: Callable[[int], tuple[int, int, int]] = _read_proc_identity_from_directory_fd
    send_signal: Callable[[int, int, int], None] = _pidfd_send_signal
    close: Callable[[int], None] = os.close


def signal_process_group_stable(
    pid: int,
    pgid: int,
    process_start_ticks: int,
    signum: int,
    *,
    deps: StableProcessSignalDeps | None = None,
) -> bool:
    """Signal a verified process group through a stable ``/proc/<pid>`` pidfd.

    ``True`` means the kernel accepted ``PIDFD_SIGNAL_PROCESS_GROUP``. On
    kernels predating that flag, ``False`` means only the stable leader target
    was signalled; callers must fail closed if descendants remain.
    """
    if any(type(value) is not int or value <= 0 for value in (pid, pgid, process_start_ticks)):
        raise StableProcessSignalError("Invalid expected process identity")
    active_deps = deps or StableProcessSignalDeps()
    try:
        process_fd = active_deps.open_process(pid)
    except OSError as exc:
        raise StableProcessSignalError(f"Cannot open a stable pidfd for pid={pid}") from exc
    try:
        try:
            observed_identity = active_deps.read_identity(process_fd)
        except (OSError, UnicodeError, ValueError) as exc:
            raise StableProcessSignalError(
                f"Cannot verify the stable process identity for pid={pid}"
            ) from exc
        expected_identity = (pid, pgid, process_start_ticks)
        if observed_identity != expected_identity:
            raise StableProcessSignalError(
                "Stable process identity changed before signalling: "
                f"expected={expected_identity} observed={observed_identity}"
            )
        try:
            active_deps.send_signal(process_fd, signum, PIDFD_SIGNAL_PROCESS_GROUP)
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise StableProcessSignalError(
                    f"Cannot signal stable process group pgid={pgid}"
                ) from exc
            try:
                active_deps.send_signal(process_fd, signum, 0)
            except OSError as fallback_exc:
                raise StableProcessSignalError(
                    f"Cannot signal stable process leader pid={pid}"
                ) from fallback_exc
            return False
        return True
    finally:
        active_deps.close(process_fd)


def current_pid_payload(
    *,
    now_fn: Callable[[], str],
    process_start_ticks_fn: Callable[[int], int | None],
    pid_fn: Callable[[], int] = os.getpid,
    boot_id_fn: Callable[[], str | None] | None = None,
) -> dict[str, int | str]:
    pid = pid_fn()
    payload: dict[str, int | str] = {
        "pid": pid,
        "started_at": now_fn(),
    }
    ticks = process_start_ticks_fn(pid)
    if ticks is not None:
        payload["process_start_ticks"] = ticks
    boot_id = boot_id_fn() if boot_id_fn is not None else None
    if isinstance(boot_id, str) and boot_id.strip():
        payload["boot_id"] = boot_id.strip()
    return payload


def read_pid_payload(pid_path: Path) -> tuple[int | None, int | None, str | None]:
    try:
        text = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None, None, None

    pid = positive_int(text)
    if pid is not None:
        return pid, None, None

    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None, None, None
    if not isinstance(raw, dict):
        return None, None, None

    raw_boot_id = raw.get("boot_id")
    boot_id = raw_boot_id.strip() if isinstance(raw_boot_id, str) and raw_boot_id.strip() else None
    return (
        positive_int(raw.get("pid")),
        positive_int(raw.get("process_start_ticks")),
        boot_id,
    )


def remove_file_silent(path: Path) -> None:
    with suppress(OSError):
        path.unlink()


def read_live_pid_file(
    pid_path: Path,
    *,
    is_process_alive_fn: Callable[[int], bool],
    process_start_ticks_fn: Callable[[int], int | None],
    boot_id_fn: Callable[[], str | None] = linux_boot_id,
    remove_file_fn: Callable[[Path], None] = remove_file_silent,
) -> int | None:
    if not pid_path.exists():
        return None
    pid, expected_ticks, expected_boot_id = read_pid_payload(pid_path)
    if pid is None:
        return None
    if expected_boot_id is not None:
        observed_boot_id = boot_id_fn()
        if observed_boot_id is not None and observed_boot_id.strip() != expected_boot_id:
            remove_file_fn(pid_path)
            return None
    if not is_process_alive_fn(pid):
        remove_file_fn(pid_path)
        return None
    if expected_ticks is not None:
        observed_ticks = process_start_ticks_fn(pid)
        if observed_ticks is None or observed_ticks != expected_ticks:
            remove_file_fn(pid_path)
            return None
    return pid


def memory_limit_preexec(
    max_memory_gb: int,
    *,
    setrlimit_fn: Callable[[int, tuple[int, int]], object],
    limit_resource: int,
) -> Callable[[], None]:
    limit_bytes = max(1, int(max_memory_gb)) * 1024 * 1024 * 1024

    def apply_limit() -> None:
        setrlimit_fn(limit_resource, (limit_bytes, limit_bytes))

    return apply_limit
