from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

from orca_auto.core.utils import process as process_utils
from orca_auto.core.utils import process_lock
from orca_auto.core.utils.persistence import (
    atomic_write_json,
    load_json_mapping_file,
    now_utc_iso,
)

ORCA_PROCESS_RECORD_FILE_NAME = "orca.process.json"


def _positive_int(value: Any) -> int | None:
    return process_utils.positive_int(value)


def orca_process_record_path(reaction_dir: Path) -> Path:
    return reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME


def write_orca_process_record(
    *,
    inp_path: Path,
    out_path: Path,
    pid: int,
) -> dict[str, Any]:
    reaction_dir = inp_path.parent
    pid_value = int(pid)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "engine": "orca",
        "pid": pid_value,
        "pgid": pid_value,
        "owner_pid": os.getpid(),
        "inp_path": str(inp_path),
        "out_path": str(out_path),
        "started_at": now_utc_iso(),
    }
    process_ticks = process_lock.process_start_ticks(pid_value)
    if process_ticks is not None:
        payload["process_start_ticks"] = process_ticks
    owner_ticks = process_lock.current_process_start_ticks()
    if owner_ticks is not None:
        payload["owner_process_start_ticks"] = owner_ticks
    atomic_write_json(orca_process_record_path(reaction_dir), payload, ensure_ascii=True, indent=2)
    return payload


def clear_orca_process_record(
    reaction_dir: Path,
    *,
    pid: int | None = None,
    process_start_ticks: int | None = None,
) -> bool:
    path = orca_process_record_path(reaction_dir)
    payload = load_json_mapping_file(path)
    if payload is None:
        return False
    if pid is not None and _positive_int(payload.get("pid")) != int(pid):
        return False
    recorded_ticks = _positive_int(payload.get("process_start_ticks"))
    if (
        process_start_ticks is not None
        and recorded_ticks is not None
        and recorded_ticks != int(process_start_ticks)
    ):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _process_group_exists(
    pgid: int,
    *,
    killpg_fn: Any,
) -> bool:
    try:
        killpg_fn(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_group_is_alive(pgid: int, *, killpg_fn: Any = os.killpg) -> bool:
    """True while any process in ``pgid`` survives (probe via ``killpg(pgid, 0)``).

    The record must outlive the group, not just its leader: the runner clears
    it only once this is False, and crash recovery reaps whatever it finds.
    """
    return _process_group_exists(pgid, killpg_fn=killpg_fn)


def _recorded_process_is_reused(
    *,
    pid: int,
    expected_ticks: int | None,
    is_process_alive_fn: Any,
    process_start_ticks_fn: Any,
) -> bool:
    if not is_process_alive_fn(pid):
        return False
    if expected_ticks is None:
        # No recorded start-ticks (the writer omits them when /proc was
        # momentarily unreadable): reuse cannot be PROVEN, so we must not
        # assume it. Assuming reuse here would discard a genuinely live
        # orphan's record without reaping it, letting the next retry run
        # beside it over the same output. Treat as not-reused and let the
        # group-existence check decide whether to reap.
        return False
    observed_ticks = process_start_ticks_fn(pid)
    # Only a readable start-tick that MISMATCHES proves reuse. A missing
    # observed tick (the leader exited between the alive check and this read,
    # while PAL/child processes keep the group alive) is not proof — assuming
    # reuse would clear the record without reaping the surviving group.
    return observed_ticks is not None and observed_ticks != expected_ticks


def _orphan_group_still_matches(
    *,
    pid: int,
    pgid: int,
    expected_ticks: int | None,
    killpg_fn: Any,
    is_process_alive_fn: Any,
    process_start_ticks_fn: Any,
) -> bool:
    if not _process_group_exists(pgid, killpg_fn=killpg_fn):
        return False
    return not _recorded_process_is_reused(
        pid=pid,
        expected_ticks=expected_ticks,
        is_process_alive_fn=is_process_alive_fn,
        process_start_ticks_fn=process_start_ticks_fn,
    )


def _wait_for_orphan_group_exit(
    *,
    pid: int,
    pgid: int,
    expected_ticks: int | None,
    timeout_seconds: float,
    killpg_fn: Any,
    is_process_alive_fn: Any,
    process_start_ticks_fn: Any,
    monotonic_fn: Any,
    sleep_fn: Any,
) -> bool:
    deadline = monotonic_fn() + max(0.0, float(timeout_seconds))
    while True:
        if not _orphan_group_still_matches(
            pid=pid,
            pgid=pgid,
            expected_ticks=expected_ticks,
            killpg_fn=killpg_fn,
            is_process_alive_fn=is_process_alive_fn,
            process_start_ticks_fn=process_start_ticks_fn,
        ):
            return True
        if monotonic_fn() >= deadline:
            return False
        sleep_fn(0.1)


def _signal_orphan_group(
    *,
    pgid: int,
    signum: int,
    killpg_fn: Any,
    logger: logging.Logger,
) -> bool:
    try:
        killpg_fn(pgid, signum)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise RuntimeError(
            f"Cannot terminate orphaned ORCA process group pgid={pgid}: permission denied"
        ) from exc
    except OSError as exc:
        logger.warning("Failed to signal orphaned ORCA process group pgid=%d: %s", pgid, exc)
        return False
    return True


def recover_orphaned_orca_process(
    reaction_dir: Path,
    *,
    logger: logging.Logger,
    graceful_timeout: float = 3.0,
    kill_timeout: float = 5.0,
    killpg_fn: Any = os.killpg,
    is_process_alive_fn: Any = process_lock.is_process_alive,
    process_start_ticks_fn: Any = process_lock.process_start_ticks,
    monotonic_fn: Any = time.monotonic,
    sleep_fn: Any = time.sleep,
) -> bool:
    path = orca_process_record_path(reaction_dir)
    payload = load_json_mapping_file(path)
    if payload is None:
        return False

    pid = _positive_int(payload.get("pid"))
    pgid = _positive_int(payload.get("pgid"))
    expected_ticks = _positive_int(payload.get("process_start_ticks"))
    if pid is None or pgid is None or pgid != pid:
        logger.warning("Ignoring invalid ORCA process record: %s", path)
        clear_orca_process_record(reaction_dir)
        return False

    if _recorded_process_is_reused(
        pid=pid,
        expected_ticks=expected_ticks,
        is_process_alive_fn=is_process_alive_fn,
        process_start_ticks_fn=process_start_ticks_fn,
    ):
        logger.info("Ignoring stale ORCA process record due to PID reuse: %s", path)
        clear_orca_process_record(reaction_dir)
        return False

    if not _process_group_exists(pgid, killpg_fn=killpg_fn):
        clear_orca_process_record(reaction_dir)
        return False

    logger.warning("Recovering orphaned ORCA process group pgid=%d for %s", pgid, reaction_dir)
    _signal_orphan_group(pgid=pgid, signum=signal.SIGTERM, killpg_fn=killpg_fn, logger=logger)
    if _wait_for_orphan_group_exit(
        pid=pid,
        pgid=pgid,
        expected_ticks=expected_ticks,
        timeout_seconds=graceful_timeout,
        killpg_fn=killpg_fn,
        is_process_alive_fn=is_process_alive_fn,
        process_start_ticks_fn=process_start_ticks_fn,
        monotonic_fn=monotonic_fn,
        sleep_fn=sleep_fn,
    ):
        clear_orca_process_record(reaction_dir, pid=pid, process_start_ticks=expected_ticks)
        return True

    _signal_orphan_group(pgid=pgid, signum=signal.SIGKILL, killpg_fn=killpg_fn, logger=logger)
    if _wait_for_orphan_group_exit(
        pid=pid,
        pgid=pgid,
        expected_ticks=expected_ticks,
        timeout_seconds=kill_timeout,
        killpg_fn=killpg_fn,
        is_process_alive_fn=is_process_alive_fn,
        process_start_ticks_fn=process_start_ticks_fn,
        monotonic_fn=monotonic_fn,
        sleep_fn=sleep_fn,
    ):
        clear_orca_process_record(reaction_dir, pid=pid, process_start_ticks=expected_ticks)
        return True

    raise RuntimeError(f"Orphaned ORCA process group is still active: pgid={pgid}")


__all__ = [
    "ORCA_PROCESS_RECORD_FILE_NAME",
    "clear_orca_process_record",
    "orca_process_record_path",
    "process_group_is_alive",
    "recover_orphaned_orca_process",
    "write_orca_process_record",
]
