from __future__ import annotations

from dataclasses import asdict, dataclass

ENGINE_PROCESS_PENDING = "pending"
ENGINE_PROCESS_ACTIVE = "active"
ENGINE_PROCESS_IDLE = "idle"
SLOT_STATE_RESERVED = "reserved"
SLOT_STATE_ACTIVE = "active"
# The worker reserves a slot under its own source; the child's activation
# rewrites the source to this value.
ADMISSION_SOURCE_QUEUE_RUN = "queue_run"


@dataclass(frozen=True, kw_only=True)
class AdmissionSlot:
    token: str
    owner_pid: int
    process_start_ticks: int
    owner_boot_id: str
    source: str
    acquired_at: str
    app_name: str = ""
    task_id: str = ""
    state: str = SLOT_STATE_ACTIVE
    work_dir: str = ""
    queue_id: str = ""
    engine_process_state: str = ENGINE_PROCESS_IDLE
    engine_launch_gated: bool = False
    engine_pid: int | None = None
    engine_pgid: int | None = None
    engine_process_start_ticks: int | None = None
    engine_process_boot_id: str | None = None


def slot_to_dict(slot: AdmissionSlot) -> dict[str, object]:
    return asdict(slot)


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
    if state not in {ENGINE_PROCESS_PENDING, ENGINE_PROCESS_ACTIVE, ENGINE_PROCESS_IDLE}:
        raise ValueError(f"Invalid admission engine process state: {state!r}")

    pid = _positive_optional_int(raw, "engine_pid")
    pgid = _positive_optional_int(raw, "engine_pgid")
    start_ticks = _positive_optional_int(raw, "engine_process_start_ticks")
    boot_id = _nonempty_optional_string(raw, "engine_process_boot_id")
    if state == ENGINE_PROCESS_ACTIVE:
        if pid is None or pgid is None or start_ticks is None or pgid != pid:
            raise ValueError("Invalid active admission engine process identity")
    elif any(value is not None for value in (pid, pgid, start_ticks, boot_id)):
        raise ValueError("Inactive admission engine process record contains an identity")
    return state, pid, pgid, start_ticks, boot_id


def slot_from_dict(raw: dict[str, object]) -> AdmissionSlot:
    expected_fields = set(AdmissionSlot.__dataclass_fields__)
    # Records written before launch-gate ownership was persisted are direct by
    # default.  Treating them as gated would make same-boot recovery discard a
    # potentially running process in the Popen-to-record interval.
    legacy_optional_fields = {"engine_launch_gated"}
    missing = expected_fields - set(raw) - legacy_optional_fields
    unknown = set(raw) - expected_fields
    if missing or unknown:
        raise ValueError(
            "Admission slot fields do not match the canonical schema: "
            f"missing={sorted(missing)} unknown={sorted(unknown)}"
        )
    engine_state, engine_pid, engine_pgid, engine_start_ticks, engine_boot_id = (
        _engine_process_fields(raw)
    )
    owner_pid_raw = raw.get("owner_pid")
    if type(owner_pid_raw) is not int or owner_pid_raw <= 0:
        raise ValueError("Invalid admission owner PID")
    owner_start_ticks = _positive_optional_int(raw, "process_start_ticks")
    owner_boot_id = _nonempty_optional_string(raw, "owner_boot_id")
    if owner_start_ticks is None or owner_boot_id is None:
        raise ValueError("Admission slot owner identity is incomplete")
    if engine_state == ENGINE_PROCESS_ACTIVE and engine_boot_id is None:
        raise ValueError("Active admission engine boot identity is incomplete")
    if engine_state == ENGINE_PROCESS_ACTIVE and owner_boot_id != engine_boot_id:
        raise ValueError("Admission owner and engine process boot IDs do not match")
    engine_launch_gated = raw.get("engine_launch_gated", False)
    if type(engine_launch_gated) is not bool:
        raise ValueError("Invalid admission engine launch-gated flag")
    return AdmissionSlot(
        token=str(raw.get("token", "")).strip(),
        owner_pid=owner_pid_raw,
        process_start_ticks=owner_start_ticks,
        source=str(raw.get("source", "")).strip(),
        acquired_at=str(raw.get("acquired_at", "")).strip(),
        owner_boot_id=owner_boot_id,
        app_name=str(raw.get("app_name", "")).strip(),
        task_id=str(raw.get("task_id", "")).strip(),
        state=str(raw.get("state", SLOT_STATE_ACTIVE)).strip() or SLOT_STATE_ACTIVE,
        work_dir=str(raw.get("work_dir", "")).strip(),
        queue_id=str(raw.get("queue_id", "")).strip(),
        engine_process_state=engine_state,
        engine_launch_gated=engine_launch_gated,
        engine_pid=engine_pid,
        engine_pgid=engine_pgid,
        engine_process_start_ticks=engine_start_ticks,
        engine_process_boot_id=engine_boot_id,
    )
