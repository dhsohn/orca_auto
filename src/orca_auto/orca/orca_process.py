from __future__ import annotations

import errno
import json
import logging
import os
import signal
import time
from pathlib import Path
from secrets import token_hex
from typing import Any

from orca_auto.core.utils import process as process_utils
from orca_auto.core.utils import process_lock
from orca_auto.core.utils.lock import file_lock
from orca_auto.core.utils.persistence import (
    atomic_write_json,
    load_json_mapping_file,
    now_utc_iso,
)

ORCA_PROCESS_RECORD_FILE_NAME = "orca.process.json"
ORCA_PROCESS_RECORD_LOCK_FILE_NAME = ".orca.process.lock"


class _ExpectationUnset:
    pass


_EXPECTATION_UNSET = _ExpectationUnset()


class OrcaProcessRecoveryError(RuntimeError):
    """Raised when legacy ORCA recovery cannot prove cleanup is safe."""


class OrcaProcessRecordCorruptError(OrcaProcessRecoveryError):
    """Raised when an existing ORCA process record cannot be trusted."""

    def __init__(self, message: str, *, record_payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.record_payload = dict(record_payload) if record_payload is not None else None


def _positive_int(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _nonempty_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def orca_process_record_path(reaction_dir: Path) -> Path:
    return reaction_dir / ORCA_PROCESS_RECORD_FILE_NAME


# Lock ordering invariant: reaction-dir run lock (when used) must be acquired
# before this process-record lock. Helpers in this module never acquire the run
# lock, so write/clear/recovery cannot introduce the reverse ordering.
def _orca_process_record_lock_path(reaction_dir: Path) -> Path:
    return reaction_dir / ORCA_PROCESS_RECORD_LOCK_FILE_NAME


def _load_orca_process_record(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise OrcaProcessRecordCorruptError(f"ORCA process record cannot be read: {path}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OrcaProcessRecordCorruptError(
            f"ORCA process record is not valid JSON: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise OrcaProcessRecordCorruptError(
            f"ORCA process record must contain a JSON object: {path}"
        )
    return payload


def _write_orca_process_record_unlocked(path: Path, payload: dict[str, Any]) -> None:
    try:
        atomic_write_json(path, payload, ensure_ascii=True, indent=2)
    except BaseException as exc:
        # A directory-fsync failure can happen after os.replace made this exact
        # generation visible. Preserve its identity so the runner can CAS-clear
        # it after the launched process has been confirmed gone.
        exc.__dict__["_orca_process_record_payload"] = dict(payload)
        raise


def write_orca_process_record(
    *,
    inp_path: Path,
    out_path: Path,
    pid: int,
    record_dir: Path | None = None,
) -> dict[str, Any]:
    reaction_dir = record_dir if record_dir is not None else inp_path.parent
    pid_value = int(pid)
    boot_id = process_utils.linux_boot_id(proc_root=Path("/proc"))
    payload: dict[str, Any] = {
        "schema_version": 2,
        "record_id": token_hex(16),
        "engine": "orca",
        "pid": pid_value,
        "pgid": pid_value,
        "owner_pid": os.getpid(),
        "inp_path": str(inp_path),
        "out_path": str(out_path),
        "started_at": now_utc_iso(),
    }
    process_ticks = process_lock.process_start_ticks(pid_value)
    owner_ticks = process_lock.current_process_start_ticks()
    if owner_ticks is not None:
        payload["owner_process_start_ticks"] = owner_ticks
    if boot_id is not None:
        payload["owner_boot_id"] = boot_id
        payload["process_boot_id"] = boot_id
    if process_ticks is None or boot_id is None:
        # Persist a fail-closed marker before aborting.  The runner removes it
        # only after the just-launched process is confirmed gone; otherwise a
        # later recovery attempt refuses to guess whether this PID is safe to
        # signal.
        with file_lock(_orca_process_record_lock_path(reaction_dir)):
            _write_orca_process_record_unlocked(
                orca_process_record_path(reaction_dir),
                payload,
            )
        unavailable_message = (
            "process start ticks are unavailable"
            if process_ticks is None
            else "Linux boot ID is unavailable"
        )
        raise OrcaProcessRecordCorruptError(
            f"Cannot identify launched ORCA process pid={pid_value}: {unavailable_message}",
            record_payload=payload,
        )
    payload["process_start_ticks"] = process_ticks
    with file_lock(_orca_process_record_lock_path(reaction_dir)):
        _write_orca_process_record_unlocked(
            orca_process_record_path(reaction_dir),
            payload,
        )
    return payload


def _clear_orca_process_record_unlocked(
    reaction_dir: Path,
    *,
    pid: int | None = None,
    process_start_ticks: int | None | _ExpectationUnset = _EXPECTATION_UNSET,
    process_boot_id: str | None | _ExpectationUnset = _EXPECTATION_UNSET,
    record_id: str | None | _ExpectationUnset = _EXPECTATION_UNSET,
) -> bool:
    path = orca_process_record_path(reaction_dir)
    payload = load_json_mapping_file(path)
    if payload is None:
        return False
    if pid is not None and _positive_int(payload.get("pid")) != int(pid):
        return False
    recorded_ticks = _positive_int(payload.get("process_start_ticks"))
    if process_start_ticks is not _EXPECTATION_UNSET and recorded_ticks != process_start_ticks:
        return False
    recorded_boot_id = _nonempty_string(payload.get("process_boot_id"))
    if process_boot_id is not _EXPECTATION_UNSET and recorded_boot_id != process_boot_id:
        return False
    recorded_id = _nonempty_string(payload.get("record_id"))
    if record_id is not _EXPECTATION_UNSET and recorded_id != record_id:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def clear_orca_process_record(
    reaction_dir: Path,
    *,
    pid: int | None = None,
    process_start_ticks: int | None | _ExpectationUnset = _EXPECTATION_UNSET,
    process_boot_id: str | None | _ExpectationUnset = _EXPECTATION_UNSET,
    record_id: str | None | _ExpectationUnset = _EXPECTATION_UNSET,
) -> bool:
    """CAS-clear one process record while excluding a replacement writer."""
    with file_lock(_orca_process_record_lock_path(reaction_dir)):
        return _clear_orca_process_record_unlocked(
            reaction_dir,
            pid=pid,
            process_start_ticks=process_start_ticks,
            process_boot_id=process_boot_id,
            record_id=record_id,
        )


def clear_orca_process_record_snapshot(
    reaction_dir: Path,
    payload: dict[str, Any],
    *,
    pid: int,
) -> bool:
    """CAS-clear only the exact record snapshot previously written by this caller."""
    if _positive_int(payload.get("pid")) != pid:
        return False
    return clear_orca_process_record(
        reaction_dir,
        pid=pid,
        process_start_ticks=_positive_int(payload.get("process_start_ticks")),
        process_boot_id=_nonempty_string(payload.get("process_boot_id")),
        record_id=_nonempty_string(payload.get("record_id")),
    )


def orca_process_record_snapshot_from_exception(exc: BaseException) -> dict[str, Any] | None:
    if isinstance(exc, OrcaProcessRecordCorruptError) and exc.record_payload is not None:
        return dict(exc.record_payload)
    payload = getattr(exc, "_orca_process_record_payload", None)
    return dict(payload) if isinstance(payload, dict) else None


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
    except OSError as exc:
        # Only ESRCH/ProcessLookup proves absence. Unknown probe failures must
        # preserve the record and block a concurrent replacement run.
        return exc.errno != errno.ESRCH
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
        raise OrcaProcessRecordCorruptError(
            f"Recorded ORCA leader pid={pid} is gone while its process group remains"
        )
    if expected_ticks is None:
        # No recorded start-ticks (the writer omits them when /proc was
        # momentarily unreadable): reuse cannot be PROVEN, so we must not
        # assume it. Assuming reuse here would discard a genuinely live
        # orphan's record without reaping it, letting the next retry run
        # beside it over the same output. Treat as not-reused and let the
        # group-existence check decide whether to reap.
        return False
    observed_ticks = process_start_ticks_fn(pid)
    if observed_ticks is None:
        # The PID still exists, so signalling its group is safe only after a
        # positive start-ticks match. Missing /proc identity is neither proof
        # of reuse nor proof that this is the recorded ORCA leader.
        raise OrcaProcessRecordCorruptError(
            f"Cannot verify recorded ORCA process identity for pid={pid}"
        )
    return observed_ticks != expected_ticks


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
    pid: int,
    pgid: int,
    expected_ticks: int,
    signum: int,
    secure_signal_fn: Any,
    logger: logging.Logger,
) -> None:
    try:
        group_scoped = secure_signal_fn(pid, pgid, expected_ticks, signum)
    except process_utils.StableProcessSignalError as exc:
        raise OrcaProcessRecoveryError(
            f"Cannot securely signal orphaned ORCA process group pgid={pgid}"
        ) from exc
    if not group_scoped:
        logger.warning(
            "Kernel lacks stable process-group pidfd signalling; "
            "signalled only ORCA leader pid=%d and will retain the record if descendants remain",
            pid,
        )


def _recover_orphaned_orca_process_unlocked(
    reaction_dir: Path,
    *,
    logger: logging.Logger,
    graceful_timeout: float = 3.0,
    kill_timeout: float = 5.0,
    killpg_fn: Any = os.killpg,
    is_process_alive_fn: Any = process_lock.is_process_alive,
    process_start_ticks_fn: Any = process_lock.process_start_ticks,
    boot_id_fn: Any = process_utils.linux_boot_id,
    secure_signal_fn: Any = process_utils.signal_process_group_stable,
    monotonic_fn: Any = time.monotonic,
    sleep_fn: Any = time.sleep,
) -> bool:
    path = orca_process_record_path(reaction_dir)
    payload = _load_orca_process_record(path)
    if payload is None:
        return False

    pid = _positive_int(payload.get("pid"))
    pgid = _positive_int(payload.get("pgid"))
    expected_ticks = _positive_int(payload.get("process_start_ticks"))
    expected_boot_id = _nonempty_string(payload.get("process_boot_id"))
    expected_record_id = _nonempty_string(payload.get("record_id"))
    schema_version = payload.get("schema_version")
    if (
        schema_version not in {1, 2}
        or payload.get("engine") != "orca"
        or pid is None
        or pgid is None
        or pgid != pid
        or expected_ticks is None
    ):
        raise OrcaProcessRecordCorruptError(f"Invalid ORCA process record: {path}")

    if schema_version == 1:
        # Version 1 start ticks are not boot-scoped. Absence of the numeric
        # group is enough to retire the legacy record, but a surviving group
        # is ambiguous and must never be signalled.
        if not _process_group_exists(pgid, killpg_fn=killpg_fn):
            _clear_orca_process_record_unlocked(
                reaction_dir,
                pid=pid,
                process_start_ticks=expected_ticks,
                process_boot_id=None,
                record_id=None,
            )
            return False
        raise OrcaProcessRecordCorruptError(
            f"Legacy ORCA process record has no boot-scoped identity: {path}"
        )
    if expected_boot_id is None or expected_record_id is None:
        raise OrcaProcessRecordCorruptError(f"Invalid ORCA process record: {path}")

    current_boot_id = boot_id_fn()
    if not isinstance(current_boot_id, str) or not current_boot_id.strip():
        raise OrcaProcessRecordCorruptError("Cannot verify the current Linux boot identity")
    if current_boot_id.strip() != expected_boot_id:
        logger.info("Ignoring stale ORCA process record from an earlier boot: %s", path)
        _clear_orca_process_record_unlocked(
            reaction_dir,
            pid=pid,
            process_start_ticks=expected_ticks,
            process_boot_id=expected_boot_id,
            record_id=expected_record_id,
        )
        return False

    if not _process_group_exists(pgid, killpg_fn=killpg_fn):
        _clear_orca_process_record_unlocked(
            reaction_dir,
            pid=pid,
            process_start_ticks=expected_ticks,
            process_boot_id=expected_boot_id,
            record_id=expected_record_id,
        )
        return False

    if _recorded_process_is_reused(
        pid=pid,
        expected_ticks=expected_ticks,
        is_process_alive_fn=is_process_alive_fn,
        process_start_ticks_fn=process_start_ticks_fn,
    ):
        logger.info("Ignoring stale ORCA process record due to PID reuse: %s", path)
        _clear_orca_process_record_unlocked(
            reaction_dir,
            pid=pid,
            process_start_ticks=expected_ticks,
            process_boot_id=expected_boot_id,
            record_id=expected_record_id,
        )
        return False

    logger.warning("Recovering orphaned ORCA process group pgid=%d for %s", pgid, reaction_dir)
    _signal_orphan_group(
        pid=pid,
        pgid=pgid,
        expected_ticks=expected_ticks,
        signum=signal.SIGTERM,
        secure_signal_fn=secure_signal_fn,
        logger=logger,
    )
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
        _clear_orca_process_record_unlocked(
            reaction_dir,
            pid=pid,
            process_start_ticks=expected_ticks,
            process_boot_id=expected_boot_id,
            record_id=expected_record_id,
        )
        return True

    _signal_orphan_group(
        pid=pid,
        pgid=pgid,
        expected_ticks=expected_ticks,
        signum=signal.SIGKILL,
        secure_signal_fn=secure_signal_fn,
        logger=logger,
    )
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
        _clear_orca_process_record_unlocked(
            reaction_dir,
            pid=pid,
            process_start_ticks=expected_ticks,
            process_boot_id=expected_boot_id,
            record_id=expected_record_id,
        )
        return True

    raise OrcaProcessRecoveryError(f"Orphaned ORCA process group is still active: pgid={pgid}")


def recover_orphaned_orca_process(
    reaction_dir: Path,
    *,
    logger: logging.Logger,
    graceful_timeout: float = 3.0,
    kill_timeout: float = 5.0,
    killpg_fn: Any = os.killpg,
    is_process_alive_fn: Any = process_lock.is_process_alive,
    process_start_ticks_fn: Any = process_lock.process_start_ticks,
    boot_id_fn: Any = process_utils.linux_boot_id,
    secure_signal_fn: Any = process_utils.signal_process_group_stable,
    monotonic_fn: Any = time.monotonic,
    sleep_fn: Any = time.sleep,
) -> bool:
    """Recover one orphan while fencing every process-record generation change.

    Callers normally already hold the reaction-dir run lock. The fixed lock
    order is run lock -> process-record lock; this function never reverses it.
    Holding the inner lock across load, identity checks, signalling, and clear
    makes the final pathname unlink an actual compare-and-swap operation.
    """
    with file_lock(_orca_process_record_lock_path(reaction_dir)):
        return _recover_orphaned_orca_process_unlocked(
            reaction_dir,
            logger=logger,
            graceful_timeout=graceful_timeout,
            kill_timeout=kill_timeout,
            killpg_fn=killpg_fn,
            is_process_alive_fn=is_process_alive_fn,
            process_start_ticks_fn=process_start_ticks_fn,
            boot_id_fn=boot_id_fn,
            secure_signal_fn=secure_signal_fn,
            monotonic_fn=monotonic_fn,
            sleep_fn=sleep_fn,
        )


__all__ = [
    "ORCA_PROCESS_RECORD_FILE_NAME",
    "OrcaProcessRecoveryError",
    "OrcaProcessRecordCorruptError",
    "clear_orca_process_record",
    "clear_orca_process_record_snapshot",
    "orca_process_record_path",
    "orca_process_record_snapshot_from_exception",
    "process_group_is_alive",
    "recover_orphaned_orca_process",
    "write_orca_process_record",
]
