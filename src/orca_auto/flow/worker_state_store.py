from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

from orca_auto.core.utils import (
    atomic_write_json,
    file_lock,
    now_utc_iso,
)
from orca_auto.core.utils import (
    coerce_mapping as _coerce_mapping,
)
from orca_auto.core.utils import (
    normalize_text as _normalize_text,
)

from .registry_store import (
    WorkflowRegistryCorruptError,
    _read_existing_json,
    _registry_lock_path,
)

WORKFLOW_WORKER_STATE_FILE_NAME = "workflow_worker_state.json"


def workflow_worker_state_path(workflow_root: str | Path) -> Path:
    root = Path(workflow_root).expanduser().resolve()
    return root / WORKFLOW_WORKER_STATE_FILE_NAME


def load_workflow_worker_state(workflow_root: str | Path) -> dict[str, Any]:
    path = workflow_worker_state_path(workflow_root)
    raw = _read_existing_json(path, description="Workflow worker state file", missing_default={})
    if not isinstance(raw, dict):
        raise WorkflowRegistryCorruptError(
            f"Workflow worker state file must contain a JSON object: {path}"
        )
    return {str(key): value for key, value in raw.items()}


def write_workflow_worker_state(
    workflow_root: str | Path,
    *,
    worker_session_id: str,
    status: str,
    workflow_root_path: str | Path | None = None,
    last_cycle_started_at: str = "",
    last_cycle_finished_at: str = "",
    last_heartbeat_at: str = "",
    lease_expires_at: str = "",
    interval_seconds: float | None = None,
    submit_ready: bool | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_root = Path(workflow_root).expanduser().resolve()
    resolved_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "worker_session_id": _normalize_text(worker_session_id),
        "status": _normalize_text(status),
        "workflow_root": str(
            Path(workflow_root_path).expanduser().resolve() if workflow_root_path else resolved_root
        ),
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "last_heartbeat_at": _normalize_text(last_heartbeat_at) or now_utc_iso(),
        "last_cycle_started_at": _normalize_text(last_cycle_started_at),
        "last_cycle_finished_at": _normalize_text(last_cycle_finished_at),
        "lease_expires_at": _normalize_text(lease_expires_at),
        "interval_seconds": interval_seconds,
        "submit_ready": submit_ready,
        "metadata": _coerce_mapping(metadata),
    }
    with file_lock(_registry_lock_path(resolved_root)):
        load_workflow_worker_state(resolved_root)
        atomic_write_json(
            workflow_worker_state_path(resolved_root), payload, ensure_ascii=True, indent=2
        )
    return payload


__all__ = [
    "WORKFLOW_WORKER_STATE_FILE_NAME",
    "load_workflow_worker_state",
    "workflow_worker_state_path",
    "write_workflow_worker_state",
]
