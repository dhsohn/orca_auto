from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

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
    return _admission_persistence.load_slots(
        root,
        slot_from_dict_fn=_slot_from_dict,
        corrupt_error=AdmissionStoreCorruptError,
    )


def _save_slots(root: Path, slots: list[AdmissionSlot]) -> None:
    _admission_persistence.save_slots(root, slots, slot_to_dict_fn=_slot_to_dict)


def _slot_owner_alive(slot: AdmissionSlot) -> bool:
    if slot.owner_pid <= 0:
        return False
    permission_denied = False
    try:
        os.kill(slot.owner_pid, 0)
    except PermissionError:
        permission_denied = True
    except OSError:
        return False
    expected = slot.process_start_ticks
    if expected is None:
        return True
    observed = _process_start_ticks(slot.owner_pid)
    if observed is None:
        return permission_denied
    return observed == expected


def _normalize_work_dir_set(work_dirs: set[str] | None) -> set[str]:
    normalized: set[str] = set()
    for work_dir in work_dirs or set():
        resolved = _normalize_work_dir(work_dir)
        if resolved:
            normalized.add(resolved)
    return normalized


def _counted_slots(
    slots: list[AdmissionSlot],
    *,
    exclude_work_dirs: set[str],
) -> tuple[list[AdmissionSlot], set[str]]:
    counted: list[AdmissionSlot] = []
    represented_work_dirs: set[str] = set()
    for slot in slots:
        if slot.work_dir and slot.work_dir in exclude_work_dirs:
            continue
        counted.append(slot)
        if slot.work_dir:
            represented_work_dirs.add(slot.work_dir)
    return counted, represented_work_dirs


def _reservation_limit_reached(
    resolved_root: Path,
    slots: list[AdmissionSlot],
    request: AdmissionReservationRequest,
) -> bool:
    excluded = _normalize_work_dir_set(request.exclude_work_dirs)
    counted, represented_work_dirs = _counted_slots(slots, exclude_work_dirs=excluded)
    extra_active_count = (
        request.extra_active_count_fn(resolved_root, represented_work_dirs, excluded)
        if request.extra_active_count_fn is not None
        else 0
    )
    return len(counted) + extra_active_count >= max(1, int(request.limit))


def _slot_from_reservation_request(request: AdmissionReservationRequest) -> AdmissionSlot:
    resolved_owner_pid = request.owner_pid if request.owner_pid is not None else os.getpid()
    return AdmissionSlot(
        token=timestamped_token("slot"),
        owner_pid=resolved_owner_pid,
        process_start_ticks=_process_start_ticks(resolved_owner_pid),
        source=request.source.strip(),
        acquired_at=now_utc_iso(),
        app_name=request.app_name.strip(),
        task_id=request.task_id.strip(),
        workflow_id=request.workflow_id.strip(),
        state=request.state.strip() or "active",
        work_dir=_normalize_work_dir(request.work_dir),
        queue_id=request.queue_id.strip(),
    )


def _activated_slot(slot: AdmissionSlot, update: AdmissionSlotActivation) -> AdmissionSlot:
    resolved_owner_pid = update.owner_pid if update.owner_pid is not None else os.getpid()
    return replace(
        slot,
        state=update.state.strip() or slot.state or "active",
        work_dir=slot.work_dir if update.work_dir is None else _normalize_work_dir(update.work_dir),
        queue_id=slot.queue_id if update.queue_id is None else update.queue_id.strip(),
        owner_pid=resolved_owner_pid,
        process_start_ticks=_process_start_ticks(resolved_owner_pid),
        source=slot.source if update.source is None else update.source.strip(),
        app_name=slot.app_name if update.app_name is None else update.app_name.strip(),
        task_id=slot.task_id if update.task_id is None else update.task_id.strip(),
        workflow_id=slot.workflow_id if update.workflow_id is None else update.workflow_id.strip(),
    )


def _metadata_updated_slot(
    slot: AdmissionSlot,
    update: AdmissionSlotMetadataUpdate,
) -> AdmissionSlot:
    resolved_owner_pid = update.owner_pid if update.owner_pid is not None else slot.owner_pid
    return replace(
        slot,
        state=slot.state if update.state is None else update.state.strip() or slot.state,
        queue_id=slot.queue_id if update.queue_id is None else update.queue_id.strip(),
        app_name=slot.app_name if update.app_name is None else update.app_name.strip(),
        task_id=slot.task_id if update.task_id is None else update.task_id.strip(),
        workflow_id=slot.workflow_id if update.workflow_id is None else update.workflow_id.strip(),
        work_dir=slot.work_dir if update.work_dir is None else _normalize_work_dir(update.work_dir),
        owner_pid=resolved_owner_pid,
        process_start_ticks=(
            slot.process_start_ticks
            if update.owner_pid is None
            else _process_start_ticks(resolved_owner_pid)
        ),
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
    exclude_work_dirs: set[str] | None = None,
    extra_active_count_fn: Callable[[Path, set[str], set[str]], int] | None = None,
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
            exclude_work_dirs=exclude_work_dirs,
            extra_active_count_fn=extra_active_count_fn,
        ),
    )


def reserve_slot_from_request(
    root: str | Path,
    request: AdmissionReservationRequest,
) -> str | None:
    store = AdmissionStore.for_root(root)

    def reserve(slots: list[AdmissionSlot]) -> tuple[str | None, bool]:
        if _reservation_limit_reached(store.root, slots, request):
            return None, True
        slot = _slot_from_reservation_request(request)
        slots.append(slot)
        return slot.token, True

    return store.mutate_live_slots(reserve)


def reserve_slot_or_raise(
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
    exclude_work_dirs: set[str] | None = None,
    extra_active_count_fn: Callable[[Path, set[str], set[str]], int] | None = None,
) -> str:
    token = reserve_slot(
        root,
        limit,
        source=source,
        app_name=app_name,
        task_id=task_id,
        workflow_id=workflow_id,
        state=state,
        work_dir=work_dir,
        queue_id=queue_id,
        owner_pid=owner_pid,
        exclude_work_dirs=exclude_work_dirs,
        extra_active_count_fn=extra_active_count_fn,
    )
    if token is None:
        raise AdmissionLimitReachedError(f"Admission limit reached (limit={max(1, int(limit))})")
    return token


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
        kept = [slot for slot in slots if slot.token != token]
        removed = len(kept) != len(slots)
        if removed:
            slots[:] = kept
        return removed, removed

    return AdmissionStore.for_root(root).mutate_live_slots(release)


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
