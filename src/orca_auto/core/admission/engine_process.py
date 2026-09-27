from __future__ import annotations

import errno
import logging
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from orca_auto.core.utils import process as process_utils

from .records import (
    ENGINE_PROCESS_ACTIVE,
    ENGINE_PROCESS_IDLE,
    ENGINE_PROCESS_PENDING,
    AdmissionSlot,
)
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
    """Raised when a pending launch record cannot be resolved yet.

    The owner may still be alive between Popen and record publication, the
    owner may be dead with no launch gate to prove that no engine ran, or the
    record may have changed under the recovery attempt.
    """


def _recorded_engine_identity_status(slot: AdmissionSlot) -> Literal["matching", "stale"]:
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
    observed_boot_id = process_utils.linux_boot_id()
    if observed_boot_id is None:
        raise EngineProcessRecordError("Cannot verify the current Linux boot identity")
    if observed_boot_id != expected_boot_id:
        # A reboot proves the recorded engine cannot still exist. A group now
        # using the same numeric PGID belongs to a different boot and must not
        # receive a signal.
        return "stale"
    try:
        os.kill(pid, 0)
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
    observed_ticks = process_utils.process_start_ticks(pid)
    if observed_ticks is None:
        raise EngineProcessRecordError(
            f"Cannot verify recorded engine leader pid={pid}: start ticks unavailable"
        )
    return "stale" if observed_ticks != expected_ticks else "matching"


def _matching_engine_group_exists(slot: AdmissionSlot) -> bool:
    if slot.engine_pgid is None:
        raise EngineProcessRecordError(
            f"Admission slot {slot.token} has no recorded engine process group"
        )
    if not process_utils.process_group_exists(slot.engine_pgid):
        return False
    return _recorded_engine_identity_status(slot) == "matching"


def _clear_record(
    root: str | Path,
    slot: AdmissionSlot,
    *,
    next_state: str = ENGINE_PROCESS_IDLE,
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
        if current is None or current.engine_process_state == ENGINE_PROCESS_IDLE:
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
        if (
            current.engine_process_state == ENGINE_PROCESS_ACTIVE
            and current_identity == expected_identity
        ):
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
) -> None:
    """Publish or clear the engine process group recorded on the child's slot.

    ``OrcaRunner`` calls it with the launched process once the launch gate holds
    the engine back, and with ``None`` after the group has exited.
    """
    if running is None:
        slot = get_slot(root, token)
        if slot is None:
            raise EngineProcessRecordError(f"Admission slot disappeared: {token}")
        if slot.engine_process_state == ENGINE_PROCESS_IDLE:
            return
        if slot.engine_process_state == ENGINE_PROCESS_PENDING:
            completed = complete_slot_engine_process(root, token)
            if completed is None:
                raise EngineProcessRecordError(f"Admission slot disappeared: {token}")
            return
        if _matching_engine_group_exists(slot):
            raise EngineProcessRecordError(
                f"Engine process group is still active for admission slot {token}"
            )
        _clear_record(root, slot)
        return

    process = getattr(running, "process", None)
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        raise EngineProcessRecordError("Running engine job has no valid process PID")
    process_start_ticks = process_utils.process_start_ticks(pid)
    if process_start_ticks is None:
        # The slot remains in its pre-launch pending state. If this child is
        # killed before cleanup completes, capacity therefore remains fenced.
        raise EngineProcessRecordError(
            f"Cannot identify launched engine process pid={pid}: start ticks unavailable"
        )
    process_boot_id = process_utils.linux_boot_id()
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


def _signal_group(slot: AdmissionSlot, signum: int) -> None:
    pgid = slot.engine_pgid
    pid = slot.engine_pid
    process_start_ticks = slot.engine_process_start_ticks
    if pgid is None or pid is None or process_start_ticks is None:
        raise EngineProcessRecordError(
            f"Admission slot {slot.token} has an incomplete engine process identity"
        )
    try:
        group_scoped = process_utils.signal_process_group_stable(
            pid, pgid, process_start_ticks, signum
        )
    except process_utils.StableProcessSignalError as exc:
        raise EngineProcessRecordError(
            f"Cannot securely signal engine process group pgid={pgid}"
        ) from exc
    if not group_scoped:
        LOGGER.warning(
            "Kernel lacks stable process-group pidfd signalling; "
            "signalled only leader pid=%s and will retain the slot if descendants remain",
            pid,
        )


def _wait_for_group_exit(slot: AdmissionSlot, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        if not _matching_engine_group_exists(slot):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _clear_dead_owner_pending_launch(root: str | Path, slot: AdmissionSlot) -> str | None:
    """Clear one pending launch whose owner is dead, or say why it must stay.

    A launch-gated record proves no engine ran: the gate wrapper execs the
    engine only after the child publishes the record and writes the release
    byte, and a child that died first closed that pipe.  A cross-boot record
    cannot describe a live engine either.  Any other pending record may hide an
    unidentified engine and is retained.
    """
    current_boot_id = process_utils.linux_boot_id()
    cross_boot = (
        slot.owner_boot_id is not None
        and current_boot_id is not None
        and slot.owner_boot_id != current_boot_id
    )
    if not cross_boot and not slot.engine_launch_gated:
        return f"Dead slot owner left a pending engine launch: token={slot.token}"
    recovery_kind = "cross-boot" if cross_boot else "launch-gated"
    try:
        completed = complete_slot_engine_process(
            root,
            slot.token,
            expected_owner_pid=slot.owner_pid,
            expected_owner_process_start_ticks=slot.process_start_ticks,
            expected_owner_boot_id=slot.owner_boot_id,
            expected_engine_launch_gated=slot.engine_launch_gated,
            require_pending_without_engine_identity=True,
        )
    except (OSError, TypeError, ValueError) as exc:
        return f"Cannot clear {recovery_kind} pending engine launch for token={slot.token}: {exc}"
    if completed is None:
        return (
            f"{recovery_kind.capitalize()} pending engine record changed while clearing "
            f"token={slot.token}"
        )
    return None


def recover_slot_engine_process(
    root: str | Path,
    token: str,
    *,
    graceful_timeout: float = 3.0,
    kill_timeout: float = 5.0,
) -> bool:
    """Reap one dead child's recorded engine group before releasing its slot."""
    slot = get_slot(root, token)
    if slot is None or slot.engine_process_state == ENGINE_PROCESS_IDLE:
        return False
    if slot.engine_process_state == ENGINE_PROCESS_PENDING:
        if process_utils.process_identity_alive(
            slot.owner_pid, slot.process_start_ticks, slot.owner_boot_id
        ):
            raise EngineProcessRecordPendingError(
                f"Admission slot {token} is pending under a live owner"
            )
        # The owner died with its launch pending.  Resolving that here, and not
        # only in the periodic orphan sweep, matters because a parent that
        # retries this recovery for a finished child suspends that sweep.
        error = _clear_dead_owner_pending_launch(root, slot)
        if error is not None:
            raise EngineProcessRecordPendingError(error)
        LOGGER.warning(
            "Cleared pending engine launch left by a dead slot owner: token=%s",
            token,
        )
        return False
    if slot.engine_pgid is None:
        raise EngineProcessRecordError(
            f"Admission slot {token} has no recorded engine process group"
        )
    if not process_utils.process_group_exists(slot.engine_pgid):
        _clear_record(root, slot)
        return False
    identity_status = _recorded_engine_identity_status(slot)
    if identity_status == "stale":
        LOGGER.info(
            "Clearing stale engine process record after process identity reuse: token=%s pid=%s",
            token,
            slot.engine_pid,
        )
        _clear_record(root, slot)
        return False
    LOGGER.warning(
        "Recovering orphaned engine process group: token=%s pgid=%s",
        token,
        slot.engine_pgid,
    )
    _signal_group(slot, signal.SIGTERM)
    if not _wait_for_group_exit(slot, graceful_timeout):
        _signal_group(slot, signal.SIGKILL)
        if not _wait_for_group_exit(slot, kill_timeout):
            raise EngineProcessRecordError(
                f"Engine process group is still active: pgid={slot.engine_pgid}"
            )
    _clear_record(root, slot)
    return True


def recover_orphaned_engine_slots(root: str | Path, *, strict: bool) -> int:
    """Recover dead-owner engine groups before generic stale-slot cleanup."""
    recovered = 0
    orphaned: list[AdmissionSlot] = []
    for slot in list_all_slots(root):
        if process_utils.process_identity_alive(
            slot.owner_pid, slot.process_start_ticks, slot.owner_boot_id
        ):
            continue
        orphaned.append(slot)

    errors: list[str] = []
    # Recover every trustworthy active record first. One ambiguous pending
    # launch must not leave unrelated, fully identified engine groups running.
    for slot in orphaned:
        if slot.engine_process_state != ENGINE_PROCESS_ACTIVE:
            continue
        try:
            recover_slot_engine_process(root, slot.token)
        except EngineProcessRecordError as exc:
            errors.append(str(exc))
        else:
            recovered += 1
    for slot in orphaned:
        if slot.engine_process_state == ENGINE_PROCESS_PENDING:
            error = _clear_dead_owner_pending_launch(root, slot)
            if error is not None:
                errors.append(error)
            else:
                recovered += 1
    if errors:
        message = "; ".join(errors)
        if strict:
            raise EngineProcessRecordError(message)
        LOGGER.error(
            "Retaining unrecovered admission engine slot(s): %s",
            message,
        )
    return recovered


__all__ = [
    "EngineProcessRecordError",
    "EngineProcessRecordPendingError",
    "build_slot_engine_process_preparer",
    "build_slot_engine_process_registrar",
    "recover_orphaned_engine_slots",
    "recover_slot_engine_process",
    "register_slot_engine_process",
]
