from __future__ import annotations

import errno
import json
import os
import signal
from contextlib import suppress
from pathlib import Path
from typing import Literal

from .coercion import positive_int as _positive_int
from .persistence import now_utc_iso

PIDFD_SIGNAL_PROCESS_GROUP = 1 << 2


class StableProcessSignalError(RuntimeError):
    """Raised when a process cannot be signalled through a stable pidfd target."""


def stat_starttime_field(stat_text: str) -> str | None:
    """Field 22 (starttime) of one ``/proc/<pid>/stat`` line, as written.

    comm is parenthesized and may contain spaces and ')': the fields after the
    final ')' start at field 3, so starttime is the twentieth of them.
    """
    right_paren = stat_text.rfind(")")
    if right_paren < 0:
        return None
    fields = stat_text[right_paren + 1 :].split()
    return fields[19] if len(fields) > 19 else None


def parse_stat_start_ticks(stat_text: str) -> int | None:
    """The positive start ticks of one ``/proc/<pid>/stat`` line, or ``None``."""
    field = stat_starttime_field(stat_text)
    if field is None:
        return None
    try:
        value = int(field)
    except ValueError:
        return None
    return value if value > 0 else None


def process_start_ticks(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    if pid <= 0:
        return None
    try:
        text = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    return parse_stat_start_ticks(text)


def linux_boot_id(*, proc_root: Path = Path("/proc")) -> str | None:
    """Return the Linux boot identifier used to scope process start ticks."""
    try:
        value = (proc_root / "sys/kernel/random/boot_id").read_text(encoding="utf-8")
    except OSError:
        return None
    normalized = value.strip()
    return normalized or None


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def process_group_exists(pgid: int) -> bool:
    """Whether a process group exists; only ESRCH proves absence."""
    try:
        os.killpg(int(pgid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


OwnerIdentityState = Literal["live", "stale", "unknown"]


def owner_identity_state(pid: int, ticks: int, boot_id: str) -> OwnerIdentityState:
    """Is the process recorded as ``(pid, ticks, boot_id)`` still the one running?

    ``stale`` needs proof: another boot, no such pid, or other start ticks. An
    unreadable boot id or start ticks is ``unknown``, which each owner record
    maps to its own policy at the call site.
    """
    current_boot_id = linux_boot_id()
    if current_boot_id is None:
        return "unknown"
    if current_boot_id != boot_id:
        return "stale"
    if not is_process_alive(pid):
        return "stale"
    observed_ticks = process_start_ticks(pid)
    if observed_ticks is None:
        return "unknown"
    return "live" if observed_ticks == ticks else "stale"


def process_identity_alive(
    pid: int,
    expected_ticks: int | None,
    expected_boot_id: str | None,
) -> bool:
    """The admission slot owner's policy: retain unless the identity proves it stale.

    Unlike :func:`owner_identity_state`, an unreadable boot id or start ticks
    and an unexpected ``kill`` errno all count as alive, so recovery never
    advances to process-group signalling on a guess.
    """
    if pid <= 0:
        return False
    if expected_boot_id is not None:
        observed_boot_id = linux_boot_id()
        if observed_boot_id is None:
            return True
        if observed_boot_id != expected_boot_id:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError as exc:
        return exc.errno != errno.ESRCH
    if expected_ticks is None:
        return True
    observed_ticks = process_start_ticks(pid)
    if observed_ticks is None:
        return True
    return observed_ticks == expected_ticks


def current_process_start_ticks() -> int | None:
    return process_start_ticks(os.getpid())


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
    signal.pidfd_send_signal(pidfd, signum, None, flags)


def signal_process_group_stable(
    pid: int,
    pgid: int,
    process_start_ticks: int,
    signum: int,
) -> bool:
    """Signal a verified process group through a stable ``/proc/<pid>`` pidfd.

    ``True`` means the kernel accepted ``PIDFD_SIGNAL_PROCESS_GROUP``. On
    kernels predating that flag, ``False`` means only the stable leader target
    was signalled; callers must fail closed if descendants remain.
    """
    if any(type(value) is not int or value <= 0 for value in (pid, pgid, process_start_ticks)):
        raise StableProcessSignalError("Invalid expected process identity")
    try:
        process_fd = _open_proc_pid_directory(pid)
    except OSError as exc:
        raise StableProcessSignalError(f"Cannot open a stable pidfd for pid={pid}") from exc
    try:
        try:
            observed_identity = _read_proc_identity_from_directory_fd(process_fd)
        except (OSError, ValueError) as exc:
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
            _pidfd_send_signal(process_fd, signum, PIDFD_SIGNAL_PROCESS_GROUP)
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise StableProcessSignalError(
                    f"Cannot signal stable process group pgid={pgid}"
                ) from exc
            try:
                _pidfd_send_signal(process_fd, signum, 0)
            except OSError as fallback_exc:
                raise StableProcessSignalError(
                    f"Cannot signal stable process leader pid={pid}"
                ) from fallback_exc
            return False
        return True
    finally:
        os.close(process_fd)


def current_pid_payload() -> dict[str, int | str]:
    pid = os.getpid()
    ticks = process_start_ticks(pid)
    boot_id = linux_boot_id()
    if ticks is None or not isinstance(boot_id, str) or not boot_id.strip():
        raise RuntimeError("Cannot determine the current boot-scoped process identity")
    return {
        "pid": pid,
        "started_at": now_utc_iso(),
        "process_start_ticks": ticks,
        "boot_id": boot_id.strip(),
    }


def read_pid_payload(pid_path: Path) -> tuple[int | None, int | None, str | None]:
    try:
        text = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None, None, None

    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None, None, None
    if not isinstance(raw, dict):
        return None, None, None

    raw_boot_id = raw.get("boot_id")
    boot_id = raw_boot_id.strip() if isinstance(raw_boot_id, str) and raw_boot_id.strip() else None
    pid = _positive_int(raw.get("pid"))
    ticks = _positive_int(raw.get("process_start_ticks"))
    if pid is None or ticks is None or boot_id is None:
        return None, None, None
    return pid, ticks, boot_id


def remove_file_silent(path: Path) -> None:
    with suppress(OSError):
        path.unlink()


def read_live_pid_file(pid_path: Path) -> int | None:
    """Return the recorded PID only for a proven live owner, without mutation.

    A worker can replace the file after this read. Only the worker holding its
    lifetime lock may replace or remove it; stale or unknown owners return None.
    """
    pid, expected_ticks, expected_boot_id = read_pid_payload(pid_path)
    if (
        pid is None
        or expected_ticks is None
        or expected_boot_id is None
        or owner_identity_state(pid, expected_ticks, expected_boot_id) != "live"
    ):
        return None
    return pid
