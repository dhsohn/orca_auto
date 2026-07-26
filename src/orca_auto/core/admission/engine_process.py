from __future__ import annotations

import errno
import logging
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from orca_auto.core.utils import process as process_utils

from .records import AdmissionSlot
from .store import (
    clear_slot_engine_process,
    complete_slot_engine_process,
    get_slot,
    list_all_slots,
    prepare_slot_engine_process,
    set_slot_engine_process,
)

LOGGER = logging.getLogger(__name__)


class EngineProcessRecordError(RuntimeError):
    """Raised when an admission slot cannot safely fence an engine process."""


class EngineProcessRecordPendingError(EngineProcessRecordError):
    """Raised when a dead child may have died between Popen and record publication."""


@dataclass(frozen=True)
class EngineProcessRecoveryDeps:
    killpg: Callable[[int, int], None] = os.killpg
    kill: Callable[[int, int], None] = os.kill
    secure_signal: Callable[[int, int, int, int], bool] = process_utils.signal_process_group_stable
    process_start_ticks: Callable[[int], int | None] = lambda pid: (
        process_utils.process_start_ticks(pid, proc_root=Path("/proc"))
    )
    boot_id: Callable[[], str | None] = lambda: process_utils.linux_boot_id(proc_root=Path("/proc"))
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    sigterm: int = signal.SIGTERM
    sigkill: int = signal.SIGKILL
    logger: logging.Logger = LOGGER


def _process_identity_alive(
    pid: int,
    expected_ticks: int | None,
    expected_boot_id: str | None,
    *,
    deps: EngineProcessRecoveryDeps,
) -> bool:
    if pid <= 0:
        return False
    if expected_boot_id is not None:
        observed_boot_id = deps.boot_id()
        if observed_boot_id is None:
            # Unknown boot identity cannot prove the owner stale. Retain the
            # slot so recovery does not advance to process-group signalling.
            return True
        if observed_boot_id != expected_boot_id:
            return False
    try:
        deps.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError as exc:
        return exc.errno != errno.ESRCH
    if expected_ticks is None:
        return True
    observed_ticks = deps.process_start_ticks(pid)
    if observed_ticks is None:
        # PID existence is proven but identity is temporarily unknown. Treat
        # the owner as live so startup never reaps a possibly active child.
        return True
    return observed_ticks == expected_ticks


def _recorded_engine_identity_status(
    slot: AdmissionSlot,
    *,
    deps: EngineProcessRecoveryDeps,
) -> Literal["matching", "stale"]:
    pid = slot.engine_pid
    expected_ticks = slot.engine_process_start_ticks
    expected_boot_id = slot.engine_process_boot_id
    if pid is None or expected_ticks is None:
        raise EngineProcessRecordError(
            f"Admission slot {slot.token} has an incomplete active engine identity"
        )
    if expected_boot_id is None:
        raise EngineProcessRecordError(
            f"Admission slot {slot.token} has no boot-scoped engine identity"
        )
    observed_boot_id = deps.boot_id()
    if observed_boot_id is None:
        raise EngineProcessRecordError("Cannot verify the current Linux boot identity")
    if observed_boot_id != expected_boot_id:
        # A reboot proves the recorded engine cannot still exist. A group now
        # using the same numeric PGID belongs to a different boot and must not
        # receive a signal.
        return "stale"
    try:
        deps.kill(pid, 0)
    except ProcessLookupError as exc:
        raise EngineProcessRecordError(
            f"Recorded engine leader pid={pid} is gone while its process group remains"
        ) from exc
    except PermissionError as exc:
        raise EngineProcessRecordError(
            f"Cannot verify recorded engine leader pid={pid}: permission denied"
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            raise EngineProcessRecordError(
                f"Recorded engine leader pid={pid} is gone while its process group remains"
            ) from exc
        raise EngineProcessRecordError(f"Cannot verify recorded engine leader pid={pid}") from exc
    observed_ticks = deps.process_start_ticks(pid)
    if observed_ticks is None:
        raise EngineProcessRecordError(
            f"Cannot verify recorded engine leader pid={pid}: start ticks unavailable"
        )
    return "stale" if observed_ticks != expected_ticks else "matching"


def _process_group_exists(pgid: int, *, deps: EngineProcessRecoveryDeps) -> bool:
    try:
        deps.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        # Only ESRCH proves absence. Unknown probe failures retain ownership.
        return exc.errno != errno.ESRCH
    return True


def _matching_engine_group_exists(
    slot: AdmissionSlot,
    *,
    deps: EngineProcessRecoveryDeps,
) -> bool:
    if slot.engine_pgid is None:
        raise EngineProcessRecordError(
            f"Admission slot {slot.token} has no recorded engine process group"
        )
    if not _process_group_exists(slot.engine_pgid, deps=deps):
        return False
    return _recorded_engine_identity_status(slot, deps=deps) == "matching"


def _clear_record(
    root: str | Path,
    slot: AdmissionSlot,
    *,
    next_state: str = "idle",
) -> None:
    cleared = clear_slot_engine_process(
        root,
        slot.token,
        expected_pid=slot.engine_pid,
        expected_process_start_ticks=slot.engine_process_start_ticks,
        expected_process_boot_id=slot.engine_process_boot_id,
        next_state=next_state,
    )
    if cleared is None:
        current = get_slot(root, slot.token)
        if current is None or current.engine_process_state == "idle":
            return
        current_identity = (
            current.engine_pid,
            current.engine_pgid,
            current.engine_process_start_ticks,
            current.engine_process_boot_id,
        )
        expected_identity = (
            slot.engine_pid,
            slot.engine_pgid,
            slot.engine_process_start_ticks,
            slot.engine_process_boot_id,
        )
        if current.engine_process_state == "active" and current_identity == expected_identity:
            retried = clear_slot_engine_process(
                root,
                slot.token,
                expected_pid=slot.engine_pid,
                expected_process_start_ticks=slot.engine_process_start_ticks,
                expected_process_boot_id=slot.engine_process_boot_id,
                next_state=next_state,
            )
            if retried is not None:
                return
        raise EngineProcessRecordError(
            f"Engine process record changed while clearing admission slot {slot.token}"
        )


def register_slot_engine_process(
    root: str | Path,
    token: str,
    running: Any | None,
    *,
    deps: EngineProcessRecoveryDeps | None = None,
) -> None:
    """Publish or clear the currently active engine group for a child worker.

    The callback is intentionally compatible with ``register_running_job`` so
    ranking sub-jobs update the same top-level admission slot for every launch.
    """
    active_deps = deps or EngineProcessRecoveryDeps()
    if running is None:
        slot = get_slot(root, token)
        if slot is None:
            raise EngineProcessRecordError(f"Admission slot disappeared: {token}")
        if slot.engine_process_state == "idle":
            return
        if slot.engine_process_state == "pending":
            completed = complete_slot_engine_process(root, token)
            if completed is None:
                raise EngineProcessRecordError(f"Admission slot disappeared: {token}")
            return
        if _matching_engine_group_exists(slot, deps=active_deps):
            raise EngineProcessRecordError(
                f"Engine process group is still active for admission slot {token}"
            )
        _clear_record(root, slot)
        return

    process = getattr(running, "process", None)
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        raise EngineProcessRecordError("Running engine job has no valid process PID")
    process_start_ticks = active_deps.process_start_ticks(pid)
    if process_start_ticks is None:
        # The slot remains in its pre-launch pending state. If this child is
        # killed before cleanup completes, capacity therefore remains fenced.
        raise EngineProcessRecordError(
            f"Cannot identify launched engine process pid={pid}: start ticks unavailable"
        )
    process_boot_id = active_deps.boot_id()
    if process_boot_id is None:
        raise EngineProcessRecordError(
            f"Cannot identify launched engine process pid={pid}: boot ID unavailable"
        )
    try:
        updated = set_slot_engine_process(
            root,
            token,
            pid=pid,
            pgid=pid,
            process_start_ticks=process_start_ticks,
            process_boot_id=process_boot_id,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise EngineProcessRecordError(
            f"Cannot publish engine process identity for admission slot {token}"
        ) from exc
    if updated is None:
        raise EngineProcessRecordError(f"Admission slot disappeared: {token}")


def build_slot_engine_process_registrar(
    root: str | Path,
    token: str,
) -> Callable[[Any | None], None]:
    return lambda running: register_slot_engine_process(root, token, running)


def build_slot_engine_process_preparer(
    root: str | Path,
    token: str,
) -> Callable[[], None]:
    def prepare() -> None:
        try:
            updated = prepare_slot_engine_process(root, token)
        except (OSError, TypeError, ValueError) as exc:
            raise EngineProcessRecordError(
                f"Cannot prepare engine launch for admission slot {token}"
            ) from exc
        if updated is None:
            raise EngineProcessRecordError(f"Admission slot disappeared: {token}")

    return prepare


def _signal_group(
    slot: AdmissionSlot,
    signum: int,
    *,
    deps: EngineProcessRecoveryDeps,
) -> None:
    pgid = slot.engine_pgid
    pid = slot.engine_pid
    process_start_ticks = slot.engine_process_start_ticks
    if pgid is None or pid is None or process_start_ticks is None:
        raise EngineProcessRecordError(
            f"Admission slot {slot.token} has an incomplete engine process identity"
        )
    try:
        group_scoped = deps.secure_signal(pid, pgid, process_start_ticks, signum)
    except process_utils.StableProcessSignalError as exc:
        raise EngineProcessRecordError(
            f"Cannot securely signal engine process group pgid={pgid}"
        ) from exc
    if not group_scoped:
        deps.logger.warning(
            "Kernel lacks stable process-group pidfd signalling; "
            "signalled only leader pid=%s and will retain the slot if descendants remain",
            pid,
        )


def _wait_for_group_exit(
    slot: AdmissionSlot,
    timeout_seconds: float,
    *,
    deps: EngineProcessRecoveryDeps,
) -> bool:
    deadline = deps.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        if not _matching_engine_group_exists(slot, deps=deps):
            return True
        if deps.monotonic() >= deadline:
            return False
        deps.sleep(0.1)


def recover_slot_engine_process(
    root: str | Path,
    token: str,
    *,
    graceful_timeout: float = 3.0,
    kill_timeout: float = 5.0,
    deps: EngineProcessRecoveryDeps | None = None,
) -> bool:
    """Reap one dead child's recorded engine group before releasing its slot."""
    active_deps = deps or EngineProcessRecoveryDeps()
    slot = get_slot(root, token)
    if slot is None or slot.engine_process_state == "idle":
        return False
    if slot.engine_process_state == "pending":
        raise EngineProcessRecordPendingError(
            f"Admission slot {token} is pending without a recoverable engine identity"
        )
    if slot.engine_pgid is None:
        raise EngineProcessRecordError(
            f"Admission slot {token} has no recorded engine process group"
        )
    if not _process_group_exists(slot.engine_pgid, deps=active_deps):
        _clear_record(root, slot)
        return False
    identity_status = _recorded_engine_identity_status(slot, deps=active_deps)
    if identity_status == "stale":
        active_deps.logger.info(
            "Clearing stale engine process record after process identity reuse: token=%s pid=%s",
            token,
            slot.engine_pid,
        )
        _clear_record(root, slot)
        return False
    active_deps.logger.warning(
        "Recovering orphaned engine process group: token=%s pgid=%s",
        token,
        slot.engine_pgid,
    )
    _signal_group(slot, active_deps.sigterm, deps=active_deps)
    if not _wait_for_group_exit(slot, graceful_timeout, deps=active_deps):
        _signal_group(slot, active_deps.sigkill, deps=active_deps)
        if not _wait_for_group_exit(slot, kill_timeout, deps=active_deps):
            raise EngineProcessRecordError(
                f"Engine process group is still active: pgid={slot.engine_pgid}"
            )
    _clear_record(root, slot)
    return True


def recover_orphaned_engine_slots(
    root: str | Path,
    *,
    source: str | tuple[str, ...] | None = None,
    deps: EngineProcessRecoveryDeps | None = None,
    strict: bool = True,
) -> int:
    """Recover dead-owner engine groups before generic stale-slot cleanup."""
    active_deps = deps or EngineProcessRecoveryDeps()
    allowed_sources = (
        None if source is None else {source} if isinstance(source, str) else set(source)
    )
    recovered = 0
    orphaned: list[AdmissionSlot] = []
    for slot in list_all_slots(root):
        if allowed_sources is not None and slot.source not in allowed_sources:
            continue
        if _process_identity_alive(
            slot.owner_pid,
            slot.process_start_ticks,
            slot.owner_boot_id,
            deps=active_deps,
        ):
            continue
        orphaned.append(slot)

    errors: list[str] = []
    # Recover every trustworthy active record first. One ambiguous pending
    # launch must not leave unrelated, fully identified engine groups running.
    for slot in orphaned:
        if slot.engine_process_state != "active":
            continue
        try:
            recover_slot_engine_process(root, slot.token, deps=active_deps)
        except EngineProcessRecordError as exc:
            errors.append(str(exc))
        else:
            recovered += 1
    for slot in orphaned:
        if slot.engine_process_state == "pending":
            current_boot_id = active_deps.boot_id()
            if (
                slot.owner_boot_id is not None
                and current_boot_id is not None
                and slot.owner_boot_id != current_boot_id
            ):
                try:
                    completed = complete_slot_engine_process(
                        root,
                        slot.token,
                        expected_owner_pid=slot.owner_pid,
                        expected_owner_process_start_ticks=slot.process_start_ticks,
                        expected_owner_boot_id=slot.owner_boot_id,
                        require_pending_without_engine_identity=True,
                    )
                except (OSError, TypeError, ValueError) as exc:
                    errors.append(
                        f"Cannot clear cross-boot pending engine launch "
                        f"for token={slot.token}: {exc}"
                    )
                else:
                    if completed is None:
                        errors.append(
                            f"Cross-boot pending engine record changed while clearing "
                            f"token={slot.token}"
                        )
                    else:
                        recovered += 1
                continue
            errors.append(f"Dead slot owner left a pending engine launch: token={slot.token}")
    if errors:
        message = "; ".join(errors)
        if strict:
            raise EngineProcessRecordError(message)
        active_deps.logger.error(
            "Retaining unrecovered admission engine slot(s): %s",
            message,
        )
    return recovered


__all__ = [
    "EngineProcessRecordError",
    "EngineProcessRecordPendingError",
    "EngineProcessRecoveryDeps",
    "build_slot_engine_process_preparer",
    "build_slot_engine_process_registrar",
    "recover_orphaned_engine_slots",
    "recover_slot_engine_process",
    "register_slot_engine_process",
]
