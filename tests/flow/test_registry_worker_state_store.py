from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orca_auto.flow import registry
from orca_auto.flow.registry import worker_state_store
from tests.flow.registry_test_helpers import (
    patch_file_locks as _patch_file_locks,
)
from tests.flow.registry_test_helpers import (
    patch_now_utc_iso as _patch_now_utc_iso,
)


def test_write_and_load_workflow_worker_state_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    monkeypatch.setattr(worker_state_store.os, "getpid", lambda: 4242)
    monkeypatch.setattr(worker_state_store.socket, "gethostname", lambda: "host-1")
    _patch_now_utc_iso(monkeypatch, lambda: "2026-04-19T02:00:00+00:00")

    written = registry.write_workflow_worker_state(
        tmp_path,
        worker_session_id="session-42",
        status="running",
        workflow_root_path=tmp_path / "custom_root",
        last_cycle_started_at="2026-04-19T01:00:00+00:00",
        interval_seconds=30.0,
        submit_ready=True,
        metadata={"cycle": 1},
    )
    loaded = registry.load_workflow_worker_state(tmp_path)

    assert written == {
        "worker_session_id": "session-42",
        "status": "running",
        "workflow_root": str((tmp_path / "custom_root").resolve()),
        "pid": 4242,
        "hostname": "host-1",
        "last_heartbeat_at": "2026-04-19T02:00:00+00:00",
        "last_cycle_started_at": "2026-04-19T01:00:00+00:00",
        "last_cycle_finished_at": "",
        "lease_expires_at": "",
        "interval_seconds": 30.0,
        "submit_ready": True,
        "metadata": {"cycle": 1},
    }
    assert loaded == written


def test_workflow_worker_state_coalesces_unchanged_heartbeat_until_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    monkeypatch.setattr(worker_state_store.os, "getpid", lambda: 4242)
    monkeypatch.setattr(worker_state_store.socket, "gethostname", lambda: "host-1")
    real_atomic_write_json = worker_state_store.atomic_write_json
    writes: list[dict[str, Any]] = []

    def tracked_atomic_write_json(path: Path, payload: dict[str, Any], **kwargs: Any) -> None:
        writes.append(dict(payload))
        real_atomic_write_json(path, payload, **kwargs)

    monkeypatch.setattr(worker_state_store, "atomic_write_json", tracked_atomic_write_json)
    common: dict[str, Any] = {
        "worker_session_id": "session-42",
        "status": "running",
        "workflow_root_path": tmp_path,
        "interval_seconds": 30.0,
        "submit_ready": True,
        "metadata": {"discovered_count": 1, "failed_count": 0},
        "minimum_heartbeat_interval_seconds": 60.0,
    }

    first = registry.write_workflow_worker_state(
        tmp_path,
        **common,
        last_heartbeat_at="2026-04-19T02:00:00+00:00",
        lease_expires_at="2026-04-19T02:01:00+00:00",
    )
    coalesced = registry.write_workflow_worker_state(
        tmp_path,
        **common,
        last_cycle_finished_at="2026-04-19T02:00:30+00:00",
        last_heartbeat_at="2026-04-19T02:00:30+00:00",
        lease_expires_at="2026-04-19T02:01:30+00:00",
    )
    due = registry.write_workflow_worker_state(
        tmp_path,
        **common,
        last_cycle_finished_at="2026-04-19T02:01:00+00:00",
        last_heartbeat_at="2026-04-19T02:01:00+00:00",
        lease_expires_at="2026-04-19T02:02:00+00:00",
    )

    assert len(writes) == 2
    assert coalesced == first
    assert due["last_heartbeat_at"] == "2026-04-19T02:01:00+00:00"


def test_workflow_worker_state_writes_semantic_change_before_heartbeat_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    monkeypatch.setattr(worker_state_store.os, "getpid", lambda: 4242)
    monkeypatch.setattr(worker_state_store.socket, "gethostname", lambda: "host-1")
    registry.write_workflow_worker_state(
        tmp_path,
        worker_session_id="session-42",
        status="running",
        last_heartbeat_at="2026-04-19T02:00:00+00:00",
        metadata={"failed_count": 0},
    )

    changed = registry.write_workflow_worker_state(
        tmp_path,
        worker_session_id="session-42",
        status="running",
        last_heartbeat_at="2026-04-19T02:00:10+00:00",
        metadata={"failed_count": 1},
        minimum_heartbeat_interval_seconds=60.0,
    )

    assert changed["last_heartbeat_at"] == "2026-04-19T02:00:10+00:00"
    assert registry.load_workflow_worker_state(tmp_path)["metadata"] == {"failed_count": 1}


@pytest.mark.parametrize("state_payload", ["{invalid", json.dumps(["bad"])])
def test_workflow_worker_state_corrupt_payload_raises_and_write_does_not_overwrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state_payload: str,
) -> None:
    _patch_file_locks(monkeypatch)
    state_path = registry.workflow_worker_state_path(tmp_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state_payload, encoding="utf-8")

    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.load_workflow_worker_state(tmp_path)
    with pytest.raises(registry.WorkflowRegistryCorruptError):
        registry.write_workflow_worker_state(
            tmp_path,
            worker_session_id="session-safe",
            status="running",
        )
    assert state_path.read_text(encoding="utf-8") == state_payload
