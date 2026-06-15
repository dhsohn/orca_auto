from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from ..utils.persistence import coerce_int, coerce_optional_int


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


@dataclass(frozen=True)
class AdmissionSlotMetadataUpdate:
    state: str | None = None
    queue_id: str | None = None
    app_name: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    work_dir: str | Path | None = None
    owner_pid: int | None = None


def slot_to_dict(slot: AdmissionSlot) -> dict[str, object]:
    return asdict(slot)


def slot_from_dict(raw: dict[str, object]) -> AdmissionSlot:
    return AdmissionSlot(
        token=str(raw.get("token", "")).strip(),
        owner_pid=coerce_int(raw.get("owner_pid", 0), default=0),
        process_start_ticks=coerce_optional_int(raw.get("process_start_ticks")),
        source=str(raw.get("source", "")).strip(),
        acquired_at=str(raw.get("acquired_at", "")).strip(),
        app_name=str(raw.get("app_name", "")).strip(),
        task_id=str(raw.get("task_id", "")).strip(),
        workflow_id=str(raw.get("workflow_id", "")).strip(),
        state=str(raw.get("state", "active")).strip() or "active",
        work_dir=str(raw.get("work_dir", "")).strip(),
        queue_id=str(raw.get("queue_id", "")).strip(),
    )
