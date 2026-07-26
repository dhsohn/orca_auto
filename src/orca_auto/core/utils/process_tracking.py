from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from orca_auto.core.utils import process as process_utils
from orca_auto.core.utils.lock import held_file_lock_payload
from orca_auto.core.utils.persistence import now_utc_iso

RUN_LOCK_FILE_NAME = "run.lock"


def current_process_lock_payload() -> dict[str, int | str]:
    return {"pid": os.getpid(), "started_at": now_utc_iso()}


@dataclass(frozen=True)
class RunLockStatus:
    held: bool
    pid: int | None = None
    started_at: str | None = None


def run_lock_status(
    reaction_dir: Path,
    *,
    logger: logging.Logger | None = None,
    lock_file_name: str = RUN_LOCK_FILE_NAME,
) -> RunLockStatus:
    lock_path = reaction_dir / lock_file_name
    try:
        payload = held_file_lock_payload(lock_path)
    except (OSError, UnicodeError, ValueError) as exc:
        if logger is not None:
            logger.warning("Cannot inspect %s ownership; treating it as held: %s", lock_path, exc)
        return RunLockStatus(held=True)
    if payload is None:
        return RunLockStatus(held=False)
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return RunLockStatus(held=True)
    if not isinstance(raw, dict):
        return RunLockStatus(held=True)
    pid = process_utils.positive_int(raw.get("pid"))
    started_at = raw.get("started_at")
    return RunLockStatus(
        held=True,
        pid=pid,
        started_at=started_at.strip()
        if isinstance(started_at, str) and started_at.strip()
        else None,
    )


def run_lock_is_held(
    reaction_dir: Path,
    *,
    logger: logging.Logger | None = None,
    lock_file_name: str = RUN_LOCK_FILE_NAME,
) -> bool:
    return run_lock_status(
        reaction_dir,
        logger=logger,
        lock_file_name=lock_file_name,
    ).held


def read_pid_file(pid_path: Path) -> int | None:
    return process_utils.read_live_pid_file(
        pid_path,
        is_process_alive_fn=process_utils.is_process_alive,
        process_start_ticks_fn=process_utils.process_start_ticks,
        boot_id_fn=process_utils.linux_boot_id,
        remove_file_fn=process_utils.remove_file_silent,
    )
