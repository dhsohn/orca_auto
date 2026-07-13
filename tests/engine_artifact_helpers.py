from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orca_auto.core.engines.artifacts import (
    EngineArtifactInput,
    EngineArtifactJob,
    EngineArtifactRecovery,
    EngineArtifactResources,
    EngineArtifactStatus,
    EngineArtifactTimestamps,
    build_engine_artifact_payload,
)


def artifact_payload(
    *,
    engine: str,
    job_id: str,
    job_dir: str = "",
    status: str = "completed",
    reason: str = "",
    exit_code: int | None = None,
    queue_id: str = "",
    app_name: str = "",
    task_id: str = "",
    generation: str = "",
    primary_path: str = "",
    selected_xyz_path: str = "",
    resource_request: Mapping[str, Any] | None = None,
    resource_actual: Mapping[str, Any] | None = None,
    created_at: str = "",
    started_at: str = "",
    updated_at: str = "",
    finished_at: str = "",
    recovery_pending: bool = False,
    recovery_reason: str = "",
    recovery_count: int = 0,
    resumed: bool = False,
    artifacts: Mapping[str, Any] | None = None,
    engine_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return build_engine_artifact_payload(
        engine=engine,
        job=EngineArtifactJob(
            id=job_id,
            queue_id=queue_id,
            dir=job_dir,
            app_name=app_name,
            task_id=task_id or job_id,
            generation=generation,
        ),
        status=EngineArtifactStatus(state=status, reason=reason, exit_code=exit_code),
        input=EngineArtifactInput(
            primary_path=primary_path,
            selected_xyz_path=selected_xyz_path or primary_path,
        ),
        resources=EngineArtifactResources(
            request=resource_request,
            actual=resource_actual or resource_request,
        ),
        timestamps=EngineArtifactTimestamps(
            created_at=created_at,
            started_at=started_at,
            updated_at=updated_at,
            finished_at=finished_at,
        ),
        recovery=EngineArtifactRecovery(
            pending=recovery_pending,
            reason=recovery_reason,
            count=recovery_count,
            resumed=resumed,
        ),
        artifacts=artifacts,
        engine_payload=engine_payload,
    )


def job(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("job")
    return dict(value) if isinstance(value, dict) else {}


def status(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("status")
    return dict(value) if isinstance(value, dict) else {}


def input_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("input")
    return dict(value) if isinstance(value, dict) else {}


def resources(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("resources")
    return dict(value) if isinstance(value, dict) else {}


def timestamps(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("timestamps")
    return dict(value) if isinstance(value, dict) else {}


def recovery(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("recovery")
    return dict(value) if isinstance(value, dict) else {}


def artifacts(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("artifacts")
    return dict(value) if isinstance(value, dict) else {}


def engine_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("engine_payload")
    return dict(value) if isinstance(value, dict) else {}


def orca_artifact_payload(
    *,
    job_id: str,
    run_id: str,
    reaction_dir: str,
    selected_inp: str = "",
    selected_xyz_path: str = "",
    status: str = "completed",
    reason: str = "",
    attempts: list[dict[str, Any]] | None = None,
    final_result: Mapping[str, Any] | None = None,
    max_retries: int = 0,
    queue_id: str = "",
    app_name: str = "orca_auto_orca",
    task_id: str = "",
    resource_request: Mapping[str, Any] | None = None,
    resource_actual: Mapping[str, Any] | None = None,
    engine_payload_extra: Mapping[str, Any] | None = None,
    artifacts_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    final_payload = dict(final_result or {})
    payload_artifacts = {
        "last_out_path": str(final_payload.get("last_out_path", "")).strip(),
    }
    payload_artifacts.update(dict(artifacts_extra or {}))
    payload_engine = {
        "run_id": run_id,
        "max_retries": max_retries,
        "attempts": list(attempts or []),
        "final_result": final_payload,
    }
    payload_engine.update(dict(engine_payload_extra or {}))
    return artifact_payload(
        engine="orca",
        job_id=job_id,
        queue_id=queue_id,
        app_name=app_name,
        task_id=task_id or job_id,
        job_dir=reaction_dir,
        status=status,
        reason=reason or str(final_payload.get("reason", "")).strip(),
        primary_path=selected_inp,
        selected_xyz_path=selected_xyz_path or selected_inp,
        resource_request=resource_request,
        resource_actual=resource_actual,
        artifacts=payload_artifacts,
        engine_payload=payload_engine,
    )
