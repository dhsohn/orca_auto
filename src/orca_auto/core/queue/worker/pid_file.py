"""The queue worker's pid file: one name, one payload, one place that reads and writes it."""

from __future__ import annotations

import json
import os
from pathlib import Path

from orca_auto.core.utils import process as process_utils
from orca_auto.core.utils.persistence import atomic_write_text, now_utc_iso
from orca_auto.core.utils.process_tracking import read_pid_file

WORKER_PID_FILE_NAME = "queue_worker.pid"


def current_worker_pid_payload() -> dict[str, int | str]:
    return process_utils.current_pid_payload(
        now_fn=now_utc_iso,
        process_start_ticks_fn=lambda pid: process_utils.process_start_ticks(
            pid, proc_root=Path("/proc")
        ),
        pid_fn=os.getpid,
        boot_id_fn=lambda: process_utils.linux_boot_id(proc_root=Path("/proc")),
    )


def worker_pid_file_path(allowed_root: Path | str, file_name: str = WORKER_PID_FILE_NAME) -> Path:
    return Path(allowed_root).expanduser().resolve() / file_name


def write_worker_pid_file(allowed_root: Path | str, file_name: str = WORKER_PID_FILE_NAME) -> None:
    payload = current_worker_pid_payload()
    atomic_write_text(
        worker_pid_file_path(allowed_root, file_name),
        json.dumps(payload, ensure_ascii=True) + "\n",
    )


def remove_worker_pid_file(allowed_root: Path | str, file_name: str = WORKER_PID_FILE_NAME) -> None:
    process_utils.remove_file_silent(worker_pid_file_path(allowed_root, file_name))


def read_worker_pid_file(
    allowed_root: Path | str, file_name: str = WORKER_PID_FILE_NAME
) -> int | None:
    return read_pid_file(worker_pid_file_path(allowed_root, file_name))


__all__ = [
    "WORKER_PID_FILE_NAME",
    "current_worker_pid_payload",
    "read_worker_pid_file",
    "remove_worker_pid_file",
    "worker_pid_file_path",
    "write_worker_pid_file",
]
