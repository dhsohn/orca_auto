from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from orca_auto.core.utils import process_lock
from orca_auto.core.utils.process_tracking import RUN_LOCK_FILE_NAME, current_process_lock_payload

logger = logging.getLogger(__name__)


LOCK_FILE_NAME = RUN_LOCK_FILE_NAME


def _run_lock_active_error(
    lock_pid: int, lock_info: dict[str, Any], lock_path: Path
) -> RuntimeError:
    started_at = lock_info.get("started_at")
    started = started_at if isinstance(started_at, str) and started_at else "unknown"
    return RuntimeError(
        "Another orca_auto instance is already running in this directory "
        f"(pid={lock_pid}, started_at={started}). Lock file: {lock_path}"
    )


def _run_lock_unreadable_error(lock_path: Path) -> RuntimeError:
    return RuntimeError(
        f"Lock file exists but owner PID is unreadable. Remove manually: {lock_path}"
    )


def _run_lock_stale_remove_error(lock_pid: int, lock_path: Path, exc: OSError) -> RuntimeError:
    return RuntimeError(
        f"Detected stale lock but failed to remove it (pid={lock_pid}). "
        f"Lock file: {lock_path}. error={exc}"
    )


@contextmanager
def acquire_run_lock(reaction_dir: Path) -> Iterator[None]:
    lock_path = reaction_dir / LOCK_FILE_NAME
    lock_payload = current_process_lock_payload()

    with process_lock.acquire_file_lock(
        lock_path=lock_path,
        lock_payload_obj=lock_payload,
        parse_lock_info_fn=process_lock.parse_lock_info,
        is_process_alive_fn=process_lock.is_process_alive,
        process_start_ticks_fn=process_lock.process_start_ticks,
        logger=logger,
        acquired_log_template="Lock acquired: %s",
        released_log_template="Lock released: %s",
        stale_pid_reuse_log_template=(
            "Stale lock detected due PID reuse (pid=%d, expected_ticks=%d, observed_ticks=%d): %s"
        ),
        stale_lock_log_template="Stale lock detected (pid=%d), removing: %s",
        active_lock_error_builder=_run_lock_active_error,
        unreadable_lock_error_builder=_run_lock_unreadable_error,
        stale_remove_error_builder=_run_lock_stale_remove_error,
    ):
        yield
