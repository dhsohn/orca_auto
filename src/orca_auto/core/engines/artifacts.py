from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from orca_auto.core.utils.coercion import safe_int
from orca_auto.core.utils.persistence import load_json_mapping_file

ENGINE_ARTIFACT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EngineArtifactJob:
    id: str
    queue_id: str
    dir: str
    app_name: str = ""
    task_id: str = ""
    generation: str = ""


@dataclass(frozen=True)
class EngineArtifactStatus:
    state: str
    reason: str = ""
    exit_code: int | None = None


@dataclass(frozen=True)
class EngineArtifactInput:
    primary_path: str = ""
    selected_xyz_path: str = ""


@dataclass(frozen=True)
class EngineArtifactResources:
    request: Mapping[str, Any] | None = None
    actual: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class EngineArtifactTimestamps:
    created_at: str = ""
    started_at: str = ""
    updated_at: str = ""
    finished_at: str = ""


@dataclass(frozen=True)
class EngineArtifactRecovery:
    pending: bool = False
    reason: str = ""
    count: int = 0
    resumed: bool = False


@dataclass(frozen=True)
class EngineArtifactProcess:
    worker_pid: int | None = None


def _json_safe(value: Any) -> Any:
    # String-valued enums must be unwrapped before the scalar check.  JSON
    # happens to serialize a ``str`` enum as its value, but Markdown renders a
    # nested container with Python ``repr`` and would otherwise expose
    # ``<SomeStatus.COMPLETED: 'completed'>``.
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return str(value)


def _clean_dict(values: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {str(key): _json_safe(value) for key, value in dict(values or {}).items()}


def _artifact_paths(values: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "manifest_path": "",
        "stdout_log": "",
        "stderr_log": "",
    }
    payload.update(_clean_dict(values))
    return payload


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def build_engine_artifact_payload(
    *,
    engine: str,
    job: EngineArtifactJob,
    status: EngineArtifactStatus,
    input: EngineArtifactInput | None = None,
    resources: EngineArtifactResources | None = None,
    timestamps: EngineArtifactTimestamps | None = None,
    recovery: EngineArtifactRecovery | None = None,
    process: EngineArtifactProcess | None = None,
    artifacts: Mapping[str, Any] | None = None,
    engine_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    active_input = input or EngineArtifactInput()
    active_resources = resources or EngineArtifactResources()
    active_timestamps = timestamps or EngineArtifactTimestamps()
    active_recovery = recovery or EngineArtifactRecovery()
    active_process = process or EngineArtifactProcess()
    payload = {
        "schema_version": ENGINE_ARTIFACT_SCHEMA_VERSION,
        "engine": _clean_text(engine),
        "job": {
            "id": _clean_text(job.id),
            "queue_id": _clean_text(job.queue_id),
            "dir": _clean_text(job.dir),
            "app_name": _clean_text(job.app_name),
            "task_id": _clean_text(job.task_id),
            "generation": _clean_text(job.generation),
        },
        "status": {
            "state": _clean_text(status.state),
            "reason": _clean_text(status.reason),
            "exit_code": safe_int(status.exit_code, default=None),
        },
        "input": {
            "primary_path": _clean_text(active_input.primary_path),
            "selected_xyz_path": _clean_text(active_input.selected_xyz_path),
        },
        "resources": {
            "request": _clean_dict(active_resources.request),
            "actual": _clean_dict(active_resources.actual),
        },
        "timestamps": {
            "created_at": _clean_text(active_timestamps.created_at),
            "started_at": _clean_text(active_timestamps.started_at),
            "updated_at": _clean_text(active_timestamps.updated_at),
            "finished_at": _clean_text(active_timestamps.finished_at),
        },
        "recovery": {
            "pending": bool(active_recovery.pending),
            "reason": _clean_text(active_recovery.reason),
            "count": safe_int(active_recovery.count, default=0),
            "resumed": bool(active_recovery.resumed),
        },
        "process": {
            "worker_pid": safe_int(active_process.worker_pid, default=None),
        },
        "artifacts": _artifact_paths(artifacts),
        "engine_payload": _clean_dict(engine_payload),
    }
    return payload


def load_engine_artifact_payload(path: Path) -> dict[str, Any] | None:
    payload = load_json_mapping_file(path)
    if payload is None:
        return None
    if safe_int(payload.get("schema_version"), default=-1) != ENGINE_ARTIFACT_SCHEMA_VERSION:
        return None
    if not _clean_text(payload.get("engine")):
        return None
    return payload


__all__ = [
    "ENGINE_ARTIFACT_SCHEMA_VERSION",
    "EngineArtifactInput",
    "EngineArtifactJob",
    "EngineArtifactProcess",
    "EngineArtifactRecovery",
    "EngineArtifactResources",
    "EngineArtifactStatus",
    "EngineArtifactTimestamps",
    "build_engine_artifact_payload",
    "load_engine_artifact_payload",
]
