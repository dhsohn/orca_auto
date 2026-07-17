from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.engines.artifacts import (
    EngineArtifactInput,
    EngineArtifactJob,
    EngineArtifactProcess,
    EngineArtifactRecovery,
    EngineArtifactResources,
    EngineArtifactStatus,
    EngineArtifactTimestamps,
    build_engine_artifact_payload,
)
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.core.utils import now_utc_iso

from . import job_locations
from .path_identity import validate_execution_snapshot_job_dir
from .state import write_report_json, write_report_md_lines, write_state


def _metadata(entry: Any) -> dict[str, Any]:
    raw = getattr(entry, "metadata", {})
    return dict(raw) if isinstance(raw, Mapping) else {}


def execution_snapshot(entry: Any) -> dict[str, Any]:
    raw = _metadata(entry).get("execution_snapshot")
    if not isinstance(raw, Mapping):
        raise ValueError("xTB-MD queue entry has no execution snapshot")
    return dict(raw)


def job_dir_from_entry(entry: Any) -> Path:
    raw = str(_metadata(entry).get("job_dir") or "").strip()
    if not raw:
        raise ValueError("xTB-MD queue entry has no job directory")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("xTB-MD queue entry job directory must be absolute")
    return path


def resource_request_from_entry(entry: Any) -> dict[str, int]:
    raw = _metadata(entry).get("resource_request")
    if not isinstance(raw, Mapping):
        raw = execution_snapshot(entry).get("resource_request")
    if not isinstance(raw, Mapping):
        raise ValueError("xTB-MD queue entry has no resource request")
    result: dict[str, int] = {}
    for key in ("max_cores", "max_memory_gb"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"xTB-MD queue resource {key!r} is invalid")
        result[key] = value
    return result


def ensemble_from_entry(entry: Any) -> str:
    value = str(_metadata(entry).get("ensemble") or "").strip().lower()
    if value not in {"nvt", "nve"}:
        raise ValueError("xTB-MD queue entry has an invalid ensemble")
    return value


def selected_input_from_entry(entry: Any) -> str:
    value = str(_metadata(entry).get("selected_input_xyz") or "").strip()
    if not value:
        raise ValueError("xTB-MD queue entry has no selected input snapshot")
    return value


def build_job_artifact(
    entry: Any,
    *,
    status: str,
    reason: str,
    exit_code: int | None,
    artifacts: Mapping[str, Any] | None = None,
    engine_payload: Mapping[str, Any] | None = None,
    worker_pid: int | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    metadata = _metadata(entry)
    job_dir = job_dir_from_entry(entry)
    selected = selected_input_from_entry(entry)
    resources = resource_request_from_entry(entry)
    updated_at = now_utc_iso()
    active_started_at = (
        started_at if started_at is not None else str(getattr(entry, "started_at", "") or "")
    )
    active_finished_at = (
        finished_at
        if finished_at is not None
        else (updated_at if status in {"completed", "failed", "cancelled"} else "")
    )
    snapshot = execution_snapshot(entry)
    base_engine_payload = {
        "ensemble": ensemble_from_entry(entry),
        "attempt": 1,
        "max_attempts": 1,
        "retry_supported": False,
        "resume_supported": False,
        "manifest": snapshot.get("manifest", {}),
        "derived_budget": snapshot.get("derived_budget", {}),
        "xtb_version": snapshot.get("xtb_version", {}),
    }
    base_engine_payload.update(dict(engine_payload or {}))
    base_artifacts = {
        "manifest_path": str(metadata.get("manifest_path") or ""),
        "execution_dir": str(metadata.get("execution_dir") or ""),
    }
    base_artifacts.update(dict(artifacts or {}))
    return build_engine_artifact_payload(
        engine="xtb_md",
        job=EngineArtifactJob(
            id=str(getattr(entry, "task_id", "") or ""),
            queue_id=str(getattr(entry, "queue_id", "") or ""),
            dir=str(job_dir),
            app_name=str(getattr(entry, "app_name", "") or ""),
            task_id=str(getattr(entry, "task_id", "") or ""),
            generation=queue_entry_generation_token(entry),
        ),
        status=EngineArtifactStatus(
            state=status,
            reason=reason,
            exit_code=exit_code,
        ),
        input=EngineArtifactInput(
            primary_path=selected,
            selected_xyz_path=selected,
        ),
        resources=EngineArtifactResources(request=resources, actual=resources),
        timestamps=EngineArtifactTimestamps(
            created_at=str(getattr(entry, "enqueued_at", "") or ""),
            started_at=active_started_at,
            updated_at=updated_at,
            finished_at=active_finished_at,
        ),
        recovery=EngineArtifactRecovery(
            pending=False,
            reason="",
            count=0,
            resumed=False,
        ),
        process=EngineArtifactProcess(worker_pid=worker_pid),
        artifacts=base_artifacts,
        engine_payload=base_engine_payload,
    )


def persist_job_artifact(cfg: Any, entry: Any, payload: dict[str, Any]) -> None:
    from orca_auto.core.engines.artifacts import build_engine_report_markdown

    snapshot = execution_snapshot(entry)

    def validated_job_dir() -> Path:
        path = validate_execution_snapshot_job_dir(cfg.runtime.allowed_root, snapshot)
        if path != job_dir_from_entry(entry):
            raise ValueError("xTB-MD queue job directory changed after submission")
        return path

    job_dir = validated_job_dir()
    write_state(job_dir, payload)
    write_report_json(job_dir, payload)
    write_report_md_lines(job_dir, build_engine_report_markdown(payload))
    resources = resource_request_from_entry(entry)
    state = payload.get("status")
    status = str(state.get("state") or "") if isinstance(state, Mapping) else ""
    job_locations.upsert_job_record(
        cfg,
        job_id=str(getattr(entry, "task_id", "") or ""),
        status=status,
        job_dir=job_dir,
        ensemble=ensemble_from_entry(entry),
        selected_input_xyz=selected_input_from_entry(entry),
        molecule_key=str(_metadata(entry).get("molecule_key") or ""),
        resource_request=resources,
        resource_actual=resources,
    )


def persist_failed_job(
    cfg: Any,
    entry: Any,
    *,
    reason: str,
    exit_code: int | None = None,
) -> dict[str, Any]:
    payload = build_job_artifact(
        entry,
        status="failed",
        reason=reason,
        exit_code=exit_code,
    )
    persist_job_artifact(cfg, entry, payload)
    return payload


__all__ = [
    "build_job_artifact",
    "ensemble_from_entry",
    "execution_snapshot",
    "job_dir_from_entry",
    "persist_failed_job",
    "persist_job_artifact",
    "resource_request_from_entry",
    "selected_input_from_entry",
]
