from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from orca_auto.core.utils import process as process_utils
from orca_auto.core.utils import process_lock
from orca_auto.core.utils.persistence import now_utc_iso

RUN_LOCK_FILE_NAME = "run.lock"


def current_process_lock_payload() -> dict[str, int | str]:
    return process_utils.current_pid_payload(
        now_fn=now_utc_iso,
        process_start_ticks_fn=lambda _pid: process_lock.current_process_start_ticks(),
        pid_fn=os.getpid,
    )


def active_run_lock_pid(
    reaction_dir: Path,
    *,
    logger: logging.Logger | None = None,
    lock_file_name: str = RUN_LOCK_FILE_NAME,
    on_pid_reuse: Callable[[int, int, int | None], None] | None = None,
) -> int | None:
    lock_info = process_lock.parse_lock_info(reaction_dir / lock_file_name)
    pid = lock_info.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    if not process_lock.is_process_alive(pid):
        return None

    expected_ticks = lock_info.get("process_start_ticks")
    if isinstance(expected_ticks, int) and expected_ticks > 0:
        observed_ticks = process_lock.process_start_ticks(pid)
        if observed_ticks is None or observed_ticks != expected_ticks:
            if on_pid_reuse is not None:
                on_pid_reuse(pid, expected_ticks, observed_ticks)
            elif logger is not None:
                logger.info(
                    "Ignoring stale %s due to PID reuse: reaction_dir=%s pid=%d expected=%d observed=%s",
                    lock_file_name,
                    reaction_dir,
                    pid,
                    expected_ticks,
                    observed_ticks,
                )
            return None
    return pid


def read_pid_file(pid_path: Path) -> int | None:
    return process_utils.read_live_pid_file(
        pid_path,
        is_process_alive_fn=process_lock.is_process_alive,
        process_start_ticks_fn=process_lock.process_start_ticks,
        remove_file_fn=process_utils.remove_file_silent,
    )
