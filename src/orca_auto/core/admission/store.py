"""Admission slot file: capacity reservations and engine-process ownership.

``admission_slots.json`` under one admission root records every reserved
slot; every read and write goes through :func:`admission_lock`.
:class:`AdmissionStore` is the lock/load/mutate/save primitive bound to one
root, and the module-level functions are the slot operations built on it
(reserve, activate, release, engine-process fencing, metadata updates and
the liveness-filtered reads).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Self, TypeVar

from ..utils import process as process_utils
from ..utils.lock import file_lock
from ..utils.persistence import (
    now_utc_iso,
    resolve_root_path,
    unique_timestamped_token,
)
from . import persistence as _admission_persistence
from .records import (
    ENGINE_PROCESS_ACTIVE,
    ENGINE_PROCESS_IDLE,
    ENGINE_PROCESS_PENDING,
    SLOT_STATE_ACTIVE,
    AdmissionSlot,
)

ADMISSION_FILE_NAME = _admission_persistence.ADMISSION_FILE_NAME
ADMISSION_LOCK_NAME = _admission_persistence.ADMISSION_LOCK_NAME
_MutationResultT = TypeVar("_MutationResultT")


class _ExpectationUnset:
    pass


_EXPECTATION_UNSET = _ExpectationUnset()


class AdmissionLimitReachedError(RuntimeError):
    """Raised when no additional admission slots are available."""


# The persistence layer raises this class directly; the store re-exports it so
# callers catch one class whether they import from here or from the package.
AdmissionStoreCorruptError = _admission_persistence.AdmissionStoreCorruptError


def _normalize_work_dir(value: str | Path | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except OSError:
        return text


def _load_slots(root: Path) -> list[AdmissionSlot]:
    try:
        return _admission_persistence.load_slots(root)
    except (TypeError, ValueError) as exc:
        raise AdmissionStoreCorruptError(
            "Admission slot file contains an invalid process record: "
            f"{_admission_persistence.admission_path(root)}"
        ) from exc


def _inactive_engine_process_state(value: str) -> str:
    state = str(value or "").strip().lower()
    if state not in {ENGINE_PROCESS_PENDING, ENGINE_PROCESS_IDLE}:
        raise ValueError(
            "Engine process state can be made active only with set_slot_engine_process"
        )
    return state


def _updated_inactive_engine_process_state(
    slot: AdmissionSlot,
    value: str | None,
) -> str:
    if value is None:
        return slot.engine_process_state
    state = _inactive_engine_process_state(value)
    if slot.engine_process_state == ENGINE_PROCESS_ACTIVE and state != ENGINE_PROCESS_ACTIVE:
        raise ValueError("Cannot discard an active engine identity through metadata update")
    return state


def _slot_owner_alive(slot: AdmissionSlot) -> bool:
    if process_utils.process_identity_alive(
        slot.owner_pid, slot.process_start_ticks, slot.owner_boot_id
    ):
        return True
    if slot.engine_process_state == ENGINE_PROCESS_PENDING:
        # A dead child with a pending marker may have died in the tiny
        # Popen-to-record interval.  Retain capacity instead of guessing.
        return True
    if slot.engine_process_state == ENGINE_PROCESS_ACTIVE:
        # Active identities are removed only by the child registrar or orphan
        # recovery. A generic capacity read must never race either path and
        # discard the sole durable record just because the group exited.
        return True
    return False


def _resolved_slot_ownership(
    slot: AdmissionSlot,
    *,
    owner_pid: int | None,
    engine_process_state: str | None,
    default_owner_pid: int,
) -> tuple[int, int, str, str]:
    """Resolve the owner identity a slot update may claim.

    An update either keeps the current owner or takes over an inactive slot; an
    active or pending engine slot never changes hands, and an owner whose start
    ticks or boot id cannot be observed is refused rather than trusted.
    """
    resolved_owner_pid = owner_pid if owner_pid is not None else default_owner_pid
    if type(resolved_owner_pid) is not int or resolved_owner_pid <= 0:
        raise ValueError("Admission slot owner PID must be a positive integer")
    if (
        slot.engine_process_state in {ENGINE_PROCESS_ACTIVE, ENGINE_PROCESS_PENDING}
        and resolved_owner_pid != slot.owner_pid
    ):
        raise ValueError("Cannot transfer ownership of an active or pending engine slot")
    owner_start_ticks = (
        slot.process_start_ticks
        if resolved_owner_pid == slot.owner_pid
        else process_utils.process_start_ticks(resolved_owner_pid)
    )
    resolved_engine_process_state = _updated_inactive_engine_process_state(
        slot,
        engine_process_state,
    )
    owner_boot_id: str | None
    if resolved_owner_pid == slot.owner_pid:
        owner_boot_id = slot.owner_boot_id
    else:
        owner_boot_id = process_utils.linux_boot_id()
    if owner_start_ticks is None or owner_boot_id is None:
        raise ValueError("Cannot verify admission slot owner process identity")
    return resolved_owner_pid, owner_start_ticks, owner_boot_id, resolved_engine_process_state


@contextmanager
def admission_lock(root: str | Path) -> Iterator[None]:
    resolved_root = resolve_root_path(root)
    # The store owns its directory (by default ``<runs_root>/.admission``)
    # beneath an already existing root; a missing runs root stays missing.
    resolved_root.mkdir(exist_ok=True)
    with file_lock(_admission_persistence.admission_lock_path(resolved_root)):
        yield


@dataclass(frozen=True)
class AdmissionStore:
    """Lock/load/mutate/save primitive for one admission root.

    The module-level functions are the slot operations and the public API;
    every one of them runs through this object so the lock discipline and the
    liveness filter live in one place. Callers hold an instance only to reach
    ``path`` or a non-normalizing ``list_slots`` read.
    """

    root: Path

    @classmethod
    def for_root(cls, root: str | Path) -> Self:
        return cls(root=resolve_root_path(root))

    @property
    def path(self) -> Path:
        return _admission_persistence.admission_path(self.root)

    def _load_live_slots(self) -> list[AdmissionSlot]:
        return [slot for slot in _load_slots(self.root) if _slot_owner_alive(slot)]

    def list_slots(self, *, normalize_file: bool = False) -> list[AdmissionSlot]:
        with admission_lock(self.root):
            recorded = _load_slots(self.root)
            slots = [slot for slot in recorded if _slot_owner_alive(slot)]
            # Rewrite only when a dead owner is being dropped; every worker
            # poll and reconcile reads this file, and an unchanged rewrite is
            # an fsync for nothing.
            if normalize_file and self.path.exists() and slots != recorded:
                _admission_persistence.save_slots(self.root, slots)
            return slots

    def mutate_live_slots(
        self,
        mutator: Callable[[list[AdmissionSlot]], tuple[_MutationResultT, bool]],
    ) -> _MutationResultT:
        with admission_lock(self.root):
            slots = self._load_live_slots()
            result, changed = mutator(slots)
            if changed:
                _admission_persistence.save_slots(self.root, slots)
            return result

    def mutate_all_slots(
        self,
        mutator: Callable[[list[AdmissionSlot]], tuple[_MutationResultT, bool]],
    ) -> _MutationResultT:
        """Mutate records without first discarding a dead process owner.

        Engine recovery must still be able to update a slot after its child
        owner has died and after the recorded engine group has just exited.
        """
        with admission_lock(self.root):
            slots = _load_slots(self.root)
            result, changed = mutator(slots)
            if changed:
                _admission_persistence.save_slots(self.root, slots)
            return result

    def mutate_slot_by_token(
        self,
        token: str,
        updater: Callable[[AdmissionSlot], tuple[_MutationResultT, AdmissionSlot | None]],
        *,
        missing_result: _MutationResultT,
        save_on_missing: bool = False,
    ) -> _MutationResultT:
        def mutate(slots: list[AdmissionSlot]) -> tuple[_MutationResultT, bool]:
            for index, slot in enumerate(slots):
                if slot.token != token:
                    continue
                result, updated_slot = updater(slot)
                if updated_slot is None:
                    return result, False
                slots[index] = updated_slot
                return result, True
            return missing_result, save_on_missing

        return self.mutate_live_slots(mutate)


def reconcile_stale_slots(root: str | Path) -> int:
    """Drop slots whose owner is dead; return how many were removed."""

    def reconcile(slots: list[AdmissionSlot]) -> tuple[int, bool]:
        kept = [slot for slot in slots if _slot_owner_alive(slot)]
        removed = len(slots) - len(kept)
        if removed:
            slots[:] = kept
        return removed, bool(removed)

    return AdmissionStore.for_root(root).mutate_all_slots(reconcile)


def list_slots(root: str | Path) -> list[AdmissionSlot]:
    return AdmissionStore.for_root(root).list_slots(normalize_file=True)


def list_all_slots(root: str | Path) -> list[AdmissionSlot]:
    store = AdmissionStore.for_root(root)
    with admission_lock(store.root):
        return _load_slots(store.root)


def read_active_slot_count(root: str | Path) -> int:
    """Count live admission slots without locking or mutating the store.

    Passive observers need the same conservative liveness semantics as
    :func:`list_slots`, but must not prune or rewrite durable worker state.
    Pending and active engine-process records therefore remain counted until
    the explicit recovery path resolves them.
    """

    store = AdmissionStore.for_root(root)
    return sum(1 for slot in _load_slots(store.root) if _slot_owner_alive(slot))


def get_slot(root: str | Path, token: str) -> AdmissionSlot | None:
    return next((slot for slot in list_all_slots(root) if slot.token == token), None)


def reserve_slot(
    root: str | Path,
    limit: int,
    *,
    source: str,
    app_name: str = "",
    task_id: str = "",
    state: str = SLOT_STATE_ACTIVE,
    work_dir: str | Path = "",
    queue_id: str = "",
    owner_pid: int | None = None,
    engine_process_state: str = ENGINE_PROCESS_IDLE,
    engine_launch_gated: bool = False,
) -> str | None:
    store = AdmissionStore.for_root(root)

    def reserve(slots: list[AdmissionSlot]) -> tuple[str | None, bool]:
        if len(slots) >= max(1, int(limit)):
            # Nothing changed; do not rewrite the file for a refused reservation.
            return None, False
        token = unique_timestamped_token("slot", {slot.token for slot in slots})
        if type(engine_launch_gated) is not bool:
            raise ValueError("Admission engine launch-gated flag must be a boolean")
        resolved_owner_pid = owner_pid if owner_pid is not None else os.getpid()
        if type(resolved_owner_pid) is not int or resolved_owner_pid <= 0:
            raise ValueError("Admission slot owner PID must be a positive integer")
        inactive_engine_process_state = _inactive_engine_process_state(engine_process_state)
        owner_start_ticks = process_utils.process_start_ticks(resolved_owner_pid)
        owner_boot_id = process_utils.linux_boot_id()
        if owner_start_ticks is None or owner_boot_id is None:
            raise ValueError("Cannot verify admission slot owner process identity")
        slots.append(
            AdmissionSlot(
                token=token,
                owner_pid=resolved_owner_pid,
                process_start_ticks=owner_start_ticks,
                source=source.strip(),
                acquired_at=now_utc_iso(),
                owner_boot_id=owner_boot_id,
                app_name=app_name.strip(),
                task_id=task_id.strip(),
                state=state.strip() or SLOT_STATE_ACTIVE,
                work_dir=_normalize_work_dir(work_dir),
                queue_id=queue_id.strip(),
                engine_process_state=inactive_engine_process_state,
                engine_launch_gated=engine_launch_gated,
            )
        )
        return token, True

    return store.mutate_live_slots(reserve)


def activate_reserved_slot(
    root: str | Path,
    token: str,
    *,
    state: str = SLOT_STATE_ACTIVE,
    work_dir: str | Path | None = None,
    queue_id: str | None = None,
    owner_pid: int | None = None,
    source: str | None = None,
    app_name: str | None = None,
    task_id: str | None = None,
    engine_process_state: str | None = None,
) -> AdmissionSlot | None:
    def activate(slot: AdmissionSlot) -> tuple[AdmissionSlot, AdmissionSlot]:
        (
            resolved_owner_pid,
            owner_start_ticks,
            owner_boot_id,
            resolved_engine_process_state,
        ) = _resolved_slot_ownership(
            slot,
            owner_pid=owner_pid,
            engine_process_state=engine_process_state,
            default_owner_pid=os.getpid(),
        )
        updated = replace(
            slot,
            state=state.strip() or slot.state or SLOT_STATE_ACTIVE,
            work_dir=slot.work_dir if work_dir is None else _normalize_work_dir(work_dir),
            queue_id=slot.queue_id if queue_id is None else queue_id.strip(),
            owner_pid=resolved_owner_pid,
            process_start_ticks=owner_start_ticks,
            owner_boot_id=owner_boot_id,
            source=slot.source if source is None else source.strip(),
            app_name=slot.app_name if app_name is None else app_name.strip(),
            task_id=slot.task_id if task_id is None else task_id.strip(),
            engine_process_state=resolved_engine_process_state,
        )
        return updated, updated

    return AdmissionStore.for_root(root).mutate_slot_by_token(
        token,
        activate,
        missing_result=None,
    )


def release_slot(root: str | Path, token: str) -> bool:
    def release(slots: list[AdmissionSlot]) -> tuple[bool, bool]:
        for index, slot in enumerate(slots):
            if slot.token != token:
                continue
            if slot.engine_process_state in {ENGINE_PROCESS_ACTIVE, ENGINE_PROCESS_PENDING}:
                raise RuntimeError(
                    f"Cannot release admission slot while an engine launch may be active: {token}"
                )
            del slots[index]
            return True, True
        return False, False

    return AdmissionStore.for_root(root).mutate_all_slots(release)


def set_slot_engine_process(
    root: str | Path,
    token: str,
    *,
    pid: int,
    pgid: int,
    process_start_ticks: int,
    process_boot_id: str | None = None,
) -> AdmissionSlot | None:
    if any(type(value) is not int for value in (pid, pgid, process_start_ticks)):
        raise ValueError("Invalid engine process identity")
    pid_value = pid
    pgid_value = pgid
    ticks_value = process_start_ticks
    boot_id_value = (
        process_boot_id.strip()
        if isinstance(process_boot_id, str)
        else process_utils.linux_boot_id() or ""
    )
    if pid_value <= 0 or pgid_value != pid_value or ticks_value <= 0 or not boot_id_value:
        raise ValueError("Invalid engine process identity")

    def update(slots: list[AdmissionSlot]) -> tuple[AdmissionSlot | None, bool]:
        for index, slot in enumerate(slots):
            if slot.token != token:
                continue
            existing_identity = (
                slot.engine_pid,
                slot.engine_pgid,
                slot.engine_process_start_ticks,
                slot.engine_process_boot_id,
            )
            requested_identity = (pid_value, pgid_value, ticks_value, boot_id_value)
            if (
                slot.engine_process_state == ENGINE_PROCESS_ACTIVE
                and existing_identity != requested_identity
            ):
                raise ValueError(f"Admission slot {token} already owns another engine process")
            if slot.engine_process_state not in {ENGINE_PROCESS_PENDING, ENGINE_PROCESS_ACTIVE}:
                raise ValueError(f"Admission slot {token} was not prepared before engine launch")
            if slot.owner_boot_id != boot_id_value:
                raise ValueError(
                    f"Admission slot {token} owner and engine boot identities do not match"
                )
            updated = replace(
                slot,
                engine_process_state=ENGINE_PROCESS_ACTIVE,
                engine_pid=pid_value,
                engine_pgid=pgid_value,
                engine_process_start_ticks=ticks_value,
                engine_process_boot_id=boot_id_value,
            )
            slots[index] = updated
            return updated, True
        return None, False

    return AdmissionStore.for_root(root).mutate_all_slots(update)


def prepare_slot_engine_process(root: str | Path, token: str) -> AdmissionSlot | None:
    """Fence the interval immediately before one engine Popen."""
    admission_store = AdmissionStore.for_root(root)
    current_boot_id = process_utils.linux_boot_id()
    if current_boot_id is None:
        raise ValueError("Cannot verify the current boot identity before engine launch")

    with admission_lock(admission_store.root):
        slots = _load_slots(admission_store.root)
        for index, slot in enumerate(slots):
            if slot.token != token:
                continue
            if slot.engine_process_state != ENGINE_PROCESS_IDLE:
                raise ValueError(f"Admission slot {token} is not a managed idle engine slot")
            if slot.owner_boot_id is None or slot.owner_boot_id != current_boot_id:
                raise ValueError(
                    f"Admission slot {token} has an ambiguous or stale owner boot identity"
                )
            updated = replace(
                slot,
                engine_process_state=ENGINE_PROCESS_PENDING,
                engine_pid=None,
                engine_pgid=None,
                engine_process_start_ticks=None,
                engine_process_boot_id=None,
            )
            slots[index] = updated
            try:
                _admission_persistence.save_slots(admission_store.root, slots)
            except BaseException:
                # atomic_write_json may have replaced the file before a
                # parent-directory fsync fails. Popen has not happened yet,
                # so restore only the exact pending marker created above.
                # Never erase an active identity observed during recovery.
                try:
                    visible_slots = _load_slots(admission_store.root)
                    for visible_index, visible in enumerate(visible_slots):
                        if visible.token != token:
                            continue
                        same_owner = (
                            visible.owner_pid,
                            visible.process_start_ticks,
                            visible.owner_boot_id,
                        ) == (
                            slot.owner_pid,
                            slot.process_start_ticks,
                            slot.owner_boot_id,
                        )
                        if (
                            same_owner
                            and visible.engine_process_state == ENGINE_PROCESS_PENDING
                            and visible.engine_pid is None
                            and visible.engine_pgid is None
                            and visible.engine_process_start_ticks is None
                            and visible.engine_process_boot_id is None
                        ):
                            visible_slots[visible_index] = replace(
                                visible,
                                engine_process_state=ENGINE_PROCESS_IDLE,
                            )
                            _admission_persistence.save_slots(
                                admission_store.root,
                                visible_slots,
                            )
                        break
                except BaseException:  # noqa: BLE001 - preserve the original save failure
                    pass
                raise
            return updated
        return None


def complete_slot_engine_process(
    root: str | Path,
    token: str,
    *,
    expected_owner_pid: int | _ExpectationUnset = _EXPECTATION_UNSET,
    expected_owner_process_start_ticks: int | None | _ExpectationUnset = _EXPECTATION_UNSET,
    expected_owner_boot_id: str | None | _ExpectationUnset = _EXPECTATION_UNSET,
    expected_engine_launch_gated: bool | _ExpectationUnset = _EXPECTATION_UNSET,
    require_pending_without_engine_identity: bool = False,
) -> AdmissionSlot | None:
    """Mark a normally completed child with no engine launch in flight."""

    if (
        expected_engine_launch_gated is not _EXPECTATION_UNSET
        and type(expected_engine_launch_gated) is not bool
    ):
        raise ValueError("Expected engine launch-gated flag must be a boolean")

    has_expectations = (
        any(
            value is not _EXPECTATION_UNSET
            for value in (
                expected_owner_pid,
                expected_owner_process_start_ticks,
                expected_owner_boot_id,
                expected_engine_launch_gated,
            )
        )
        or require_pending_without_engine_identity
    )

    def update(slots: list[AdmissionSlot]) -> tuple[AdmissionSlot | None, bool]:
        for index, slot in enumerate(slots):
            if slot.token != token:
                continue
            if slot.engine_process_state == ENGINE_PROCESS_ACTIVE:
                raise ValueError(f"Admission slot {token} still owns an active engine process")
            if slot.engine_process_state != ENGINE_PROCESS_PENDING:
                return (None if has_expectations else slot), False
            if (
                expected_owner_pid is not _EXPECTATION_UNSET
                and slot.owner_pid != expected_owner_pid
            ):
                return None, False
            if (
                expected_owner_process_start_ticks is not _EXPECTATION_UNSET
                and slot.process_start_ticks != expected_owner_process_start_ticks
            ):
                return None, False
            if (
                expected_owner_boot_id is not _EXPECTATION_UNSET
                and slot.owner_boot_id != expected_owner_boot_id
            ):
                return None, False
            if (
                expected_engine_launch_gated is not _EXPECTATION_UNSET
                and slot.engine_launch_gated != expected_engine_launch_gated
            ):
                return None, False
            if require_pending_without_engine_identity and any(
                value is not None
                for value in (
                    slot.engine_pid,
                    slot.engine_pgid,
                    slot.engine_process_start_ticks,
                    slot.engine_process_boot_id,
                )
            ):
                return None, False
            updated = replace(slot, engine_process_state=ENGINE_PROCESS_IDLE)
            slots[index] = updated
            return updated, True
        return None, False

    return AdmissionStore.for_root(root).mutate_all_slots(update)


def clear_slot_engine_process(
    root: str | Path,
    token: str,
    *,
    expected_pid: int | None = None,
    expected_process_start_ticks: int | None = None,
    expected_process_boot_id: str | None | _ExpectationUnset = _EXPECTATION_UNSET,
    next_state: str = ENGINE_PROCESS_IDLE,
) -> AdmissionSlot | None:
    resolved_next_state = _inactive_engine_process_state(next_state)
    if resolved_next_state not in {ENGINE_PROCESS_PENDING, ENGINE_PROCESS_IDLE}:
        raise ValueError("Cleared engine process state must be pending or idle")

    def update(slots: list[AdmissionSlot]) -> tuple[AdmissionSlot | None, bool]:
        for index, slot in enumerate(slots):
            if slot.token != token:
                continue
            if expected_pid is not None and slot.engine_pid != int(expected_pid):
                return None, False
            if expected_process_start_ticks is not None and slot.engine_process_start_ticks != int(
                expected_process_start_ticks
            ):
                return None, False
            if (
                expected_process_boot_id is not _EXPECTATION_UNSET
                and slot.engine_process_boot_id != expected_process_boot_id
            ):
                return None, False
            updated = replace(
                slot,
                engine_process_state=resolved_next_state,
                engine_pid=None,
                engine_pgid=None,
                engine_process_start_ticks=None,
                engine_process_boot_id=None,
            )
            slots[index] = updated
            return updated, True
        return None, False

    return AdmissionStore.for_root(root).mutate_all_slots(update)


def update_slot_metadata(
    root: str | Path,
    token: str,
    *,
    state: str | None = None,
    queue_id: str | None = None,
    app_name: str | None = None,
    task_id: str | None = None,
    work_dir: str | Path | None = None,
    owner_pid: int | None = None,
    engine_process_state: str | None = None,
) -> AdmissionSlot | None:
    def update_metadata(slot: AdmissionSlot) -> tuple[AdmissionSlot, AdmissionSlot]:
        (
            resolved_owner_pid,
            owner_start_ticks,
            owner_boot_id,
            resolved_engine_process_state,
        ) = _resolved_slot_ownership(
            slot,
            owner_pid=owner_pid,
            engine_process_state=engine_process_state,
            default_owner_pid=slot.owner_pid,
        )
        updated = replace(
            slot,
            state=slot.state if state is None else state.strip() or slot.state,
            queue_id=slot.queue_id if queue_id is None else queue_id.strip(),
            app_name=slot.app_name if app_name is None else app_name.strip(),
            task_id=slot.task_id if task_id is None else task_id.strip(),
            work_dir=slot.work_dir if work_dir is None else _normalize_work_dir(work_dir),
            owner_pid=resolved_owner_pid,
            process_start_ticks=owner_start_ticks,
            owner_boot_id=owner_boot_id,
            engine_process_state=resolved_engine_process_state,
        )
        return updated, updated

    return AdmissionStore.for_root(root).mutate_slot_by_token(
        token,
        update_metadata,
        missing_result=None,
        save_on_missing=True,
    )
