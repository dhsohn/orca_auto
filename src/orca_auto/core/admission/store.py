from __future__ import annotations

import errno
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeVar

from ..utils import process as process_utils
from ..utils.lock import file_lock
from ..utils.persistence import (
    now_utc_iso,
    resolve_root_path,
    timestamped_token,
)
from . import persistence as _admission_persistence
from . import records as _admission_records
from .records import (
    AdmissionReservationRequest,
    AdmissionSlot,
    AdmissionSlotActivation,
    AdmissionSlotMetadataUpdate,
)

ADMISSION_FILE_NAME = _admission_persistence.ADMISSION_FILE_NAME
ADMISSION_LOCK_NAME = _admission_persistence.ADMISSION_LOCK_NAME
_MutationResultT = TypeVar("_MutationResultT")
_TOKEN_COLLISION_RETRY_LIMIT = 32


class _ExpectationUnset:
    pass


_EXPECTATION_UNSET = _ExpectationUnset()


class AdmissionLimitReachedError(RuntimeError):
    """Raised when no additional admission slots are available."""


class AdmissionStoreCorruptError(_admission_persistence.AdmissionStoreCorruptError):
    """Raised when the admission slot file cannot be safely loaded."""


def _admission_path(root: Path) -> Path:
    return _admission_persistence.admission_path(root)


def _lock_path(root: Path) -> Path:
    return _admission_persistence.admission_lock_path(root)


def _process_start_ticks(pid: int) -> int | None:
    return process_utils.process_start_ticks(pid, proc_root=Path("/proc"))


def _linux_boot_id() -> str | None:
    return process_utils.linux_boot_id(proc_root=Path("/proc"))


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


def _slot_to_dict(slot: AdmissionSlot) -> dict[str, object]:
    return _admission_records.slot_to_dict(slot)


def _slot_from_dict(raw: dict[str, object]) -> AdmissionSlot:
    return _admission_records.slot_from_dict(raw)


def _load_slots(root: Path) -> list[AdmissionSlot]:
    try:
        return _admission_persistence.load_slots(
            root,
            slot_from_dict_fn=_slot_from_dict,
            corrupt_error=AdmissionStoreCorruptError,
        )
    except (TypeError, ValueError) as exc:
        raise AdmissionStoreCorruptError(
            f"Admission slot file contains an invalid process record: {_admission_path(root)}"
        ) from exc


def _save_slots(root: Path, slots: list[AdmissionSlot]) -> None:
    _admission_persistence.save_slots(
        root,
        slots,
        slot_to_dict_fn=_slot_to_dict,
        corrupt_error=AdmissionStoreCorruptError,
    )


def _process_identity_alive(
    pid: int,
    expected_ticks: int,
    expected_boot_id: str,
) -> bool:
    if pid <= 0:
        return False
    observed_boot_id = _linux_boot_id()
    if observed_boot_id is None:
        # An unreadable boot identity is ambiguous. Keep the slot rather than
        # declaring its owner dead and initiating engine recovery.
        return True
    if observed_boot_id != expected_boot_id:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError as exc:
        return exc.errno != errno.ESRCH
    observed = _process_start_ticks(pid)
    if observed is None:
        # A live PID whose /proc identity is temporarily unreadable is
        # unknown, not dead. Keep the slot and skip orphan recovery.
        return True
    return observed == expected_ticks


def _inactive_engine_process_state(value: str) -> str:
    state = str(value or "").strip().lower()
    if state not in {"pending", "idle"}:
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
    if slot.engine_process_state == "active" and state != "active":
        raise ValueError("Cannot discard an active engine identity through metadata update")
    return state


def _slot_owner_process_alive(slot: AdmissionSlot) -> bool:
    if slot.owner_pid <= 0:
        return False
    return _process_identity_alive(
        slot.owner_pid,
        slot.process_start_ticks,
        slot.owner_boot_id,
    )


def _slot_owner_alive(slot: AdmissionSlot) -> bool:
    if _slot_owner_process_alive(slot):
        return True
    if slot.engine_process_state == "pending":
        # A dead child with a pending marker may have died in the tiny
        # Popen-to-record interval.  Retain capacity instead of guessing.
        return True
    if slot.engine_process_state == "active":
        # Active identities are removed only by the child registrar or orphan
        # recovery. A generic capacity read must never race either path and
        # discard the sole durable record just because the group exited.
        return True
    return False


def _reservation_limit_reached(
    slots: list[AdmissionSlot],
    request: AdmissionReservationRequest,
) -> bool:
    return len(slots) >= max(1, int(request.limit))


def _slot_from_reservation_request(
    request: AdmissionReservationRequest,
    *,
    token: str,
) -> AdmissionSlot:
    if type(request.engine_launch_gated) is not bool:
        raise ValueError("Admission engine launch-gated flag must be a boolean")
    resolved_owner_pid = request.owner_pid if request.owner_pid is not None else os.getpid()
    if type(resolved_owner_pid) is not int or resolved_owner_pid <= 0:
        raise ValueError("Admission slot owner PID must be a positive integer")
    engine_process_state = _inactive_engine_process_state(request.engine_process_state)
    owner_start_ticks = _process_start_ticks(resolved_owner_pid)
    owner_boot_id = _linux_boot_id()
    if owner_start_ticks is None or owner_boot_id is None:
        raise ValueError("Cannot verify admission slot owner process identity")
    return AdmissionSlot(
        token=token,
        owner_pid=resolved_owner_pid,
        process_start_ticks=owner_start_ticks,
        source=request.source.strip(),
        acquired_at=now_utc_iso(),
        owner_boot_id=owner_boot_id,
        app_name=request.app_name.strip(),
        task_id=request.task_id.strip(),
        workflow_id=request.workflow_id.strip(),
        state=request.state.strip() or "active",
        work_dir=_normalize_work_dir(request.work_dir),
        queue_id=request.queue_id.strip(),
        engine_process_state=engine_process_state,
        engine_launch_gated=request.engine_launch_gated,
    )


def _resolved_slot_ownership(
    slot: AdmissionSlot,
    update: AdmissionSlotActivation | AdmissionSlotMetadataUpdate,
    *,
    default_owner_pid: int,
) -> tuple[int, int, str, str]:
    """Resolve the owner identity a slot update may claim.

    An update either keeps the current owner or takes over an inactive slot; an
    active or pending engine slot never changes hands, and an owner whose start
    ticks or boot id cannot be observed is refused rather than trusted.
    """
    resolved_owner_pid = update.owner_pid if update.owner_pid is not None else default_owner_pid
    if type(resolved_owner_pid) is not int or resolved_owner_pid <= 0:
        raise ValueError("Admission slot owner PID must be a positive integer")
    if slot.engine_process_state in {"active", "pending"} and resolved_owner_pid != slot.owner_pid:
        raise ValueError("Cannot transfer ownership of an active or pending engine slot")
    owner_start_ticks = (
        slot.process_start_ticks
        if resolved_owner_pid == slot.owner_pid
        else _process_start_ticks(resolved_owner_pid)
    )
    engine_process_state = _updated_inactive_engine_process_state(
        slot,
        update.engine_process_state,
    )
    owner_boot_id: str | None
    if resolved_owner_pid == slot.owner_pid:
        owner_boot_id = slot.owner_boot_id
    else:
        owner_boot_id = _linux_boot_id()
    if owner_start_ticks is None or owner_boot_id is None:
        raise ValueError("Cannot verify admission slot owner process identity")
    return resolved_owner_pid, owner_start_ticks, owner_boot_id, engine_process_state


def _activated_slot(slot: AdmissionSlot, update: AdmissionSlotActivation) -> AdmissionSlot:
    (
        resolved_owner_pid,
        owner_start_ticks,
        owner_boot_id,
        engine_process_state,
    ) = _resolved_slot_ownership(slot, update, default_owner_pid=os.getpid())
    return replace(
        slot,
        state=update.state.strip() or slot.state or "active",
        work_dir=slot.work_dir if update.work_dir is None else _normalize_work_dir(update.work_dir),
        queue_id=slot.queue_id if update.queue_id is None else update.queue_id.strip(),
        owner_pid=resolved_owner_pid,
        process_start_ticks=owner_start_ticks,
        owner_boot_id=owner_boot_id,
        source=slot.source if update.source is None else update.source.strip(),
        app_name=slot.app_name if update.app_name is None else update.app_name.strip(),
        task_id=slot.task_id if update.task_id is None else update.task_id.strip(),
        workflow_id=slot.workflow_id if update.workflow_id is None else update.workflow_id.strip(),
        engine_process_state=engine_process_state,
    )


def _metadata_updated_slot(
    slot: AdmissionSlot,
    update: AdmissionSlotMetadataUpdate,
) -> AdmissionSlot:
    (
        resolved_owner_pid,
        owner_start_ticks,
        owner_boot_id,
        engine_process_state,
    ) = _resolved_slot_ownership(slot, update, default_owner_pid=slot.owner_pid)
    return replace(
        slot,
        state=slot.state if update.state is None else update.state.strip() or slot.state,
        queue_id=slot.queue_id if update.queue_id is None else update.queue_id.strip(),
        app_name=slot.app_name if update.app_name is None else update.app_name.strip(),
        task_id=slot.task_id if update.task_id is None else update.task_id.strip(),
        workflow_id=slot.workflow_id if update.workflow_id is None else update.workflow_id.strip(),
        work_dir=slot.work_dir if update.work_dir is None else _normalize_work_dir(update.work_dir),
        owner_pid=resolved_owner_pid,
        process_start_ticks=owner_start_ticks,
        owner_boot_id=owner_boot_id,
        engine_process_state=engine_process_state,
    )


@contextmanager
def admission_lock(root: str | Path) -> Iterator[None]:
    resolved_root = resolve_root_path(root)
    with file_lock(_lock_path(resolved_root)):
        yield


@dataclass(frozen=True)
class AdmissionStore:
    """Persistence facade for one admission root.

    Module-level functions remain the public API. New code can use this object
    to keep root resolution and lock/load/save mutation semantics in one place
    instead of repeating that pattern at each call site.
    """

    root: Path
    load_slots_fn: Callable[[Path], list[AdmissionSlot]]
    save_slots_fn: Callable[[Path, list[AdmissionSlot]], Any]

    @classmethod
    def for_root(
        cls,
        root: str | Path,
        *,
        load_slots_fn: Callable[[Path], list[AdmissionSlot]] | None = None,
        save_slots_fn: Callable[[Path, list[AdmissionSlot]], Any] | None = None,
    ) -> AdmissionStore:
        return cls(
            root=resolve_root_path(root),
            load_slots_fn=load_slots_fn or _load_slots,
            save_slots_fn=save_slots_fn or _save_slots,
        )

    @property
    def path(self) -> Path:
        return _admission_path(self.root)

    def _load_live_slots(self) -> list[AdmissionSlot]:
        return [slot for slot in self.load_slots_fn(self.root) if _slot_owner_alive(slot)]

    def reconcile_stale_slots(self) -> int:
        with admission_lock(self.root):
            slots = self.load_slots_fn(self.root)
            kept = [slot for slot in slots if _slot_owner_alive(slot)]
            removed = len(slots) - len(kept)
            if removed:
                self.save_slots_fn(self.root, kept)
            return removed

    def list_slots(self, *, normalize_file: bool = False) -> list[AdmissionSlot]:
        with admission_lock(self.root):
            slots = self._load_live_slots()
            if normalize_file and self.path.exists():
                self.save_slots_fn(self.root, slots)
            return slots

    def mutate_live_slots(
        self,
        mutator: Callable[[list[AdmissionSlot]], tuple[_MutationResultT, bool]],
    ) -> _MutationResultT:
        with admission_lock(self.root):
            slots = self._load_live_slots()
            result, changed = mutator(slots)
            if changed:
                self.save_slots_fn(self.root, slots)
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
            slots = self.load_slots_fn(self.root)
            result, changed = mutator(slots)
            if changed:
                self.save_slots_fn(self.root, slots)
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
    return AdmissionStore.for_root(root).reconcile_stale_slots()


def list_slots(root: str | Path) -> list[AdmissionSlot]:
    return AdmissionStore.for_root(root).list_slots(normalize_file=True)


def list_all_slots(root: str | Path) -> list[AdmissionSlot]:
    store = AdmissionStore.for_root(root)
    with admission_lock(store.root):
        return store.load_slots_fn(store.root)


def read_active_slot_count(root: str | Path) -> int:
    """Count live admission slots without locking or mutating the store.

    Passive observers need the same conservative liveness semantics as
    :func:`list_slots`, but must not prune or rewrite durable worker state.
    Pending and active engine-process records therefore remain counted until
    the explicit recovery path resolves them.
    """

    store = AdmissionStore.for_root(root)
    return sum(1 for slot in store.load_slots_fn(store.root) if _slot_owner_alive(slot))


def get_slot(root: str | Path, token: str) -> AdmissionSlot | None:
    return next((slot for slot in list_all_slots(root) if slot.token == token), None)


def active_slot_count(root: str | Path) -> int:
    return len(list_slots(root))


def reserve_slot(
    root: str | Path,
    limit: int,
    *,
    source: str,
    app_name: str = "",
    task_id: str = "",
    workflow_id: str = "",
    state: str = "active",
    work_dir: str | Path = "",
    queue_id: str = "",
    owner_pid: int | None = None,
    engine_process_state: str = "idle",
    engine_launch_gated: bool = False,
) -> str | None:
    return reserve_slot_from_request(
        root,
        AdmissionReservationRequest(
            limit=limit,
            source=source,
            app_name=app_name,
            task_id=task_id,
            workflow_id=workflow_id,
            state=state,
            work_dir=work_dir,
            queue_id=queue_id,
            owner_pid=owner_pid,
            engine_process_state=engine_process_state,
            engine_launch_gated=engine_launch_gated,
        ),
    )


def reserve_slot_from_request(
    root: str | Path,
    request: AdmissionReservationRequest,
) -> str | None:
    store = AdmissionStore.for_root(root)

    def reserve(slots: list[AdmissionSlot]) -> tuple[str | None, bool]:
        if _reservation_limit_reached(slots, request):
            return None, True
        occupied_tokens = {slot.token for slot in slots}
        token = ""
        for _attempt in range(_TOKEN_COLLISION_RETRY_LIMIT):
            candidate = timestamped_token("slot")
            if candidate not in occupied_tokens:
                token = candidate
                break
        if not token:
            raise RuntimeError(
                "Could not allocate a unique admission slot token after "
                f"{_TOKEN_COLLISION_RETRY_LIMIT} attempts"
            )
        slot = _slot_from_reservation_request(request, token=token)
        slots.append(slot)
        return slot.token, True

    return store.mutate_live_slots(reserve)


def activate_reserved_slot(
    root: str | Path,
    token: str,
    *,
    state: str = "active",
    work_dir: str | Path | None = None,
    queue_id: str | None = None,
    owner_pid: int | None = None,
    source: str | None = None,
    app_name: str | None = None,
    task_id: str | None = None,
    workflow_id: str | None = None,
    engine_process_state: str | None = None,
) -> AdmissionSlot | None:
    return activate_reserved_slot_with_update(
        root,
        token,
        AdmissionSlotActivation(
            state=state,
            work_dir=work_dir,
            queue_id=queue_id,
            owner_pid=owner_pid,
            source=source,
            app_name=app_name,
            task_id=task_id,
            workflow_id=workflow_id,
            engine_process_state=engine_process_state,
        ),
    )


def activate_reserved_slot_with_update(
    root: str | Path,
    token: str,
    update: AdmissionSlotActivation,
) -> AdmissionSlot | None:
    def activate(slot: AdmissionSlot) -> tuple[AdmissionSlot, AdmissionSlot]:
        updated = _activated_slot(slot, update)
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
            if slot.engine_process_state in {"active", "pending"}:
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
        process_boot_id.strip() if isinstance(process_boot_id, str) else _linux_boot_id() or ""
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
            if slot.engine_process_state == "active" and existing_identity != requested_identity:
                raise ValueError(f"Admission slot {token} already owns another engine process")
            if slot.engine_process_state not in {"pending", "active"}:
                raise ValueError(f"Admission slot {token} was not prepared before engine launch")
            if slot.owner_boot_id != boot_id_value:
                raise ValueError(
                    f"Admission slot {token} owner and engine boot identities do not match"
                )
            updated = replace(
                slot,
                engine_process_state="active",
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
    current_boot_id = _linux_boot_id()
    if current_boot_id is None:
        raise ValueError("Cannot verify the current boot identity before engine launch")

    with admission_lock(admission_store.root):
        slots = admission_store.load_slots_fn(admission_store.root)
        for index, slot in enumerate(slots):
            if slot.token != token:
                continue
            if slot.engine_process_state != "idle":
                raise ValueError(f"Admission slot {token} is not a managed idle engine slot")
            if slot.owner_boot_id is None or slot.owner_boot_id != current_boot_id:
                raise ValueError(
                    f"Admission slot {token} has an ambiguous or stale owner boot identity"
                )
            updated = replace(
                slot,
                engine_process_state="pending",
                engine_pid=None,
                engine_pgid=None,
                engine_process_start_ticks=None,
                engine_process_boot_id=None,
            )
            slots[index] = updated
            try:
                admission_store.save_slots_fn(admission_store.root, slots)
            except BaseException:
                # atomic_write_json may have replaced the file before a
                # parent-directory fsync fails. Popen has not happened yet,
                # so restore only the exact pending marker created above.
                # Never erase an active identity observed during recovery.
                try:
                    visible_slots = admission_store.load_slots_fn(admission_store.root)
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
                            and visible.engine_process_state == "pending"
                            and visible.engine_pid is None
                            and visible.engine_pgid is None
                            and visible.engine_process_start_ticks is None
                            and visible.engine_process_boot_id is None
                        ):
                            visible_slots[visible_index] = replace(
                                visible,
                                engine_process_state="idle",
                            )
                            admission_store.save_slots_fn(
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
            if slot.engine_process_state == "active":
                raise ValueError(f"Admission slot {token} still owns an active engine process")
            if slot.engine_process_state != "pending":
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
            updated = replace(slot, engine_process_state="idle")
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
    next_state: str = "idle",
) -> AdmissionSlot | None:
    resolved_next_state = _inactive_engine_process_state(next_state)
    if resolved_next_state not in {"pending", "idle"}:
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
    workflow_id: str | None = None,
    work_dir: str | Path | None = None,
    owner_pid: int | None = None,
    engine_process_state: str | None = None,
) -> AdmissionSlot | None:
    return update_slot_metadata_with_update(
        root,
        token,
        AdmissionSlotMetadataUpdate(
            state=state,
            queue_id=queue_id,
            app_name=app_name,
            task_id=task_id,
            workflow_id=workflow_id,
            work_dir=work_dir,
            owner_pid=owner_pid,
            engine_process_state=engine_process_state,
        ),
    )


def update_slot_metadata_with_update(
    root: str | Path,
    token: str,
    update: AdmissionSlotMetadataUpdate,
) -> AdmissionSlot | None:
    def update_metadata(slot: AdmissionSlot) -> tuple[AdmissionSlot, AdmissionSlot]:
        updated = _metadata_updated_slot(slot, update)
        return updated, updated

    return AdmissionStore.for_root(root).mutate_slot_by_token(
        token,
        update_metadata,
        missing_result=None,
        save_on_missing=True,
    )
