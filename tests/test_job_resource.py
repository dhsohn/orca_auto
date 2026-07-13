from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orca_auto import job_resource


def _slot(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "queue_id": "q",
        "engine_pid": 100,
        "engine_pgid": 100,
        "engine_process_start_ticks": 555,
        "engine_process_boot_id": "boot-A",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _resolve(slots: list[SimpleNamespace], **overrides: Any) -> dict[str, int]:
    defaults: dict[str, Any] = {
        "engine_runtime_paths_fn": lambda cfg, engine: {"admission_root": Path("/x")},
        "read_slots_fn": lambda root: slots,
        "is_alive": lambda pid: True,
        "start_ticks": lambda pid: None,
        "boot_id": lambda: "boot-A",
    }
    defaults.update(overrides)
    return job_resource.live_job_pgids("orca_auto.yaml", **defaults)


def test_live_job_pgids_returns_validated_live_pgids() -> None:
    slots = [
        _slot(queue_id="q1", engine_pid=100, engine_pgid=111, engine_process_start_ticks=555),
        _slot(queue_id="q2", engine_pid=200, engine_pgid=222, engine_process_start_ticks=777),
    ]
    result = _resolve(slots, start_ticks=lambda pid: {100: 555, 200: 777}[pid])
    assert result == {"q1": 111, "q2": 222}


def test_live_job_pgids_drops_stale_reused_and_incomplete() -> None:
    slots = [
        _slot(queue_id="dead", engine_pid=1),  # not alive
        _slot(queue_id="otherboot", engine_process_boot_id="boot-OLD"),  # recorded on old boot
        _slot(queue_id="reused", engine_pid=300, engine_process_start_ticks=999),  # ticks mismatch
        _slot(queue_id="nopid", engine_pid=None),
        _slot(queue_id="nopgid", engine_pgid=None),
        _slot(queue_id="", engine_pgid=100),  # missing queue id
        _slot(queue_id="live", engine_pid=400, engine_pgid=444, engine_process_start_ticks=42),
    ]
    result = _resolve(
        slots,
        is_alive=lambda pid: pid != 1,
        start_ticks=lambda pid: {300: 111, 400: 42}.get(pid),
        boot_id=lambda: "boot-A",
    )
    assert result == {"live": 444}


def test_live_job_pgids_fails_closed_on_missing_config_or_root() -> None:
    assert job_resource.live_job_pgids(None) == {}
    assert job_resource.live_job_pgids("   ") == {}
    assert (
        job_resource.live_job_pgids(
            "orca_auto.yaml",
            engine_runtime_paths_fn=lambda cfg, engine: {},  # no admission_root
            read_slots_fn=lambda root: [],
        )
        == {}
    )


def test_live_job_pgids_fails_closed_when_slot_store_raises() -> None:
    def _boom(root: Any) -> list[Any]:
        raise OSError("corrupt")

    assert _resolve([], read_slots_fn=_boom) == {}


def test_read_slots_default_is_read_only(tmp_path: Path) -> None:
    # Regression: the metrics view must observe admission slots without rewriting
    # or pruning the worker's durable state (the writing `list_slots` would drop a
    # dead-owner slot and rewrite the file on every read).
    from orca_auto.core.admission import read_slots
    from orca_auto.core.admission.persistence import ADMISSION_FILE_NAME
    from orca_auto.core.admission.records import AdmissionSlot
    from orca_auto.core.admission.store import _save_slots

    dead_owner_slot = AdmissionSlot(
        token="t1",
        owner_pid=2_000_000_000,  # not a live pid
        process_start_ticks=1,
        source="orca_auto",
        acquired_at="2026-01-01T00:00:00Z",
        queue_id="q1",
    )
    _save_slots(tmp_path, [dead_owner_slot])
    path = tmp_path / ADMISSION_FILE_NAME
    before = path.read_bytes()

    slots = read_slots(tmp_path)

    assert path.read_bytes() == before  # not rewritten
    assert [slot.token for slot in slots] == ["t1"]  # dead-owner slot not pruned
