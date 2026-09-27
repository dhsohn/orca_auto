"""The queue worker's pid file: one name, one payload, one place that reads and writes it."""

from __future__ import annotations

import json
from pathlib import Path

from orca_auto.core.utils import process as process_utils
from orca_auto.core.utils.persistence import atomic_write_text

WORKER_PID_FILE_NAME = "queue_worker.pid"


def worker_pid_file_path(allowed_root: Path | str, file_name: str = WORKER_PID_FILE_NAME) -> Path:
    return Path(allowed_root).expanduser().resolve() / file_name


def write_worker_pid_file(allowed_root: Path | str, file_name: str = WORKER_PID_FILE_NAME) -> None:
    payload = process_utils.current_pid_payload()
    atomic_write_text(
        worker_pid_file_path(allowed_root, file_name),
        json.dumps(payload, ensure_ascii=True) + "\n",
    )


def remove_worker_pid_file(allowed_root: Path | str, file_name: str = WORKER_PID_FILE_NAME) -> None:
    process_utils.remove_file_silent(worker_pid_file_path(allowed_root, file_name))


def read_worker_pid_file(
    allowed_root: Path | str, file_name: str = WORKER_PID_FILE_NAME
) -> int | None:
    return process_utils.read_live_pid_file(worker_pid_file_path(allowed_root, file_name))


__all__ = [
    "WORKER_PID_FILE_NAME",
    "read_worker_pid_file",
    "remove_worker_pid_file",
    "worker_pid_file_path",
    "write_worker_pid_file",
]
