from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class AdmissionSlot:
    token: str
    owner_pid: int
    process_start_ticks: int | None
    source: str
    acquired_at: str
    app_name: str = ""
    task_id: str = ""
    workflow_id: str = ""
    state: str = "active"
    work_dir: str = ""
    queue_id: str = ""
    engine_process_state: str = ""
    engine_pid: int | None = None
    engine_pgid: int | None = None
    engine_process_start_ticks: int | None = None
    owner_boot_id: str | None = None
    engine_process_boot_id: str | None = None


@dataclass(frozen=True)
class AdmissionReservationRequest:
    limit: int
    source: str
    app_name: str = ""
    task_id: str = ""
    workflow_id: str = ""
    state: str = "active"
    work_dir: str | Path = ""
    queue_id: str = ""
    owner_pid: int | None = None
    engine_process_state: str = ""
    exclude_work_dirs: set[str] | None = None
    extra_active_count_fn: Callable[[Path, set[str], set[str]], int] | None = None


@dataclass(frozen=True)
class AdmissionSlotActivation:
    state: str = "active"
    work_dir: str | Path | None = None
    queue_id: str | None = None
    owner_pid: int | None = None
    source: str | None = None
    app_name: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    engine_process_state: str | None = None


@dataclass(frozen=True)
class AdmissionSlotMetadataUpdate:
    state: str | None = None
    queue_id: str | None = None
    app_name: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    work_dir: str | Path | None = None
    owner_pid: int | None = None
    engine_process_state: str | None = None


def slot_to_dict(slot: AdmissionSlot) -> dict[str, object]:
    payload = asdict(slot)
    if slot.owner_boot_id is None:
        payload.pop("owner_boot_id", None)
    if not slot.engine_process_state:
        payload.pop("engine_process_state", None)
        payload.pop("engine_pid", None)
        payload.pop("engine_pgid", None)
        payload.pop("engine_process_start_ticks", None)
        payload.pop("engine_process_boot_id", None)
    return payload


def _positive_optional_int(raw: dict[str, object], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ValueError(f"Invalid positive integer admission field {key!r}")
    return value


def _nonempty_optional_string(raw: dict[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid non-empty string admission field {key!r}")
    return value.strip()


def _engine_process_fields(
    raw: dict[str, object],
) -> tuple[str, int | None, int | None, int | None, str | None]:
    state = str(raw.get("engine_process_state", "") or "").strip().lower()
    engine_keys_present = any(
        key in raw
        for key in (
            "engine_process_state",
            "engine_pid",
            "engine_pgid",
            "engine_process_start_ticks",
            "engine_process_boot_id",
        )
    )
    if not engine_keys_present:
        return "", None, None, None, None
    if state not in {"pending", "active", "idle"}:
        raise ValueError(f"Invalid admission engine process state: {state!r}")

    pid = _positive_optional_int(raw, "engine_pid")
    pgid = _positive_optional_int(raw, "engine_pgid")
    start_ticks = _positive_optional_int(raw, "engine_process_start_ticks")
    boot_id = _nonempty_optional_string(raw, "engine_process_boot_id")
    if state == "active":
        if pid is None or pgid is None or start_ticks is None or pgid != pid:
            raise ValueError("Invalid active admission engine process identity")
    elif any(value is not None for value in (pid, pgid, start_ticks, boot_id)):
        raise ValueError("Inactive admission engine process record contains an identity")
    return state, pid, pgid, start_ticks, boot_id


def slot_from_dict(raw: dict[str, object]) -> AdmissionSlot:
    engine_state, engine_pid, engine_pgid, engine_start_ticks, engine_boot_id = (
        _engine_process_fields(raw)
    )
    owner_pid_raw = raw.get("owner_pid", 0)
    if type(owner_pid_raw) is not int or owner_pid_raw < 0:
        raise ValueError("Invalid admission owner PID")
    owner_start_ticks = _positive_optional_int(raw, "process_start_ticks")
    owner_boot_id = _nonempty_optional_string(raw, "owner_boot_id")
    if engine_state and (owner_pid_raw <= 0 or owner_start_ticks is None):
        raise ValueError("Managed admission slot owner identity is incomplete")
    if engine_state == "active" and (owner_boot_id is None) != (engine_boot_id is None):
        raise ValueError("Admission owner and engine boot identities are incomplete")
    if (
        engine_state == "active"
        and owner_boot_id is not None
        and engine_boot_id is not None
        and owner_boot_id != engine_boot_id
    ):
        raise ValueError("Admission owner and engine process boot IDs do not match")
    return AdmissionSlot(
        token=str(raw.get("token", "")).strip(),
        owner_pid=owner_pid_raw,
        process_start_ticks=owner_start_ticks,
        source=str(raw.get("source", "")).strip(),
        acquired_at=str(raw.get("acquired_at", "")).strip(),
        owner_boot_id=owner_boot_id,
        app_name=str(raw.get("app_name", "")).strip(),
        task_id=str(raw.get("task_id", "")).strip(),
        workflow_id=str(raw.get("workflow_id", "")).strip(),
        state=str(raw.get("state", "active")).strip() or "active",
        work_dir=str(raw.get("work_dir", "")).strip(),
        queue_id=str(raw.get("queue_id", "")).strip(),
        engine_process_state=engine_state,
        engine_pid=engine_pid,
        engine_pgid=engine_pgid,
        engine_process_start_ticks=engine_start_ticks,
        engine_process_boot_id=engine_boot_id,
    )
