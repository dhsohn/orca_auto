from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from orca_auto.core.engines.artifacts import (
    EngineArtifactInput as NormalizedArtifactInput,
)
from orca_auto.core.engines.artifacts import (
    EngineArtifactJob,
    EngineArtifactProcess,
    EngineArtifactRecovery,
    EngineArtifactResources,
    EngineArtifactStatus,
    EngineArtifactTimestamps,
    build_engine_artifact_payload,
)
from orca_auto.core.queue import execution as _queue_execution
from orca_auto.core.queue.generation import queue_entry_generation_token

from ..types import QueueEntry

TerminalResultT = TypeVar("TerminalResultT")


@dataclass(frozen=True)
class EngineArtifactFields:
    selected_input_xyz: str
    engine: str = ""
    engine_fields: Mapping[str, Any] | None = None
    detail_fields: Mapping[str, Any] | None = None

    def engine_payload(self) -> dict[str, Any]:
        return dict(self.engine_fields or {})

    def detail_payload(self) -> dict[str, Any]:
        return dict(self.detail_fields or {})


def sanitized_execution_provenance(entry: Any) -> dict[str, Any]:
    """Return durable, non-secret input and executable identity for reports."""

    metadata = getattr(entry, "metadata", {})
    if not isinstance(metadata, Mapping):
        return {}
    snapshot = metadata.get("execution_snapshot")
    if not isinstance(snapshot, Mapping):
        return {}
    descriptors = snapshot.get("source_inputs")
    if not isinstance(descriptors, Mapping):
        descriptors = snapshot.get("input_snapshots")
    materialized = snapshot.get("materialized_inputs")
    inputs: list[dict[str, Any]] = []
    if isinstance(descriptors, Mapping):
        for role, descriptor in sorted(descriptors.items(), key=lambda item: str(item[0])):
            if not isinstance(descriptor, Mapping):
                continue
            executed_path = str(descriptor.get("snapshot_path") or "")
            materialized_identity = (
                materialized.get(role) if isinstance(materialized, Mapping) else None
            )
            if isinstance(materialized_identity, Mapping):
                executed_path = str(materialized_identity.get("path") or executed_path)
            elif role == "selected_source":
                executed_path = str(snapshot.get("selected_inp") or executed_path)
            inputs.append(
                {
                    "role": str(role),
                    "source_path": str(descriptor.get("source_path") or ""),
                    "executed_path": executed_path,
                    "sha256": str(descriptor.get("sha256") or ""),
                    "size_bytes": descriptor.get("size_bytes"),
                }
            )
    executables: dict[str, dict[str, Any]] = {}
    raw_executables = snapshot.get("executable_identities")
    if isinstance(raw_executables, Mapping):
        for name, identity in sorted(raw_executables.items(), key=lambda item: str(item[0])):
            if not isinstance(identity, Mapping):
                continue
            executables[str(name)] = {
                "path": str(identity.get("path") or ""),
                "sha256": str(identity.get("sha256") or ""),
                "size_bytes": identity.get("size_bytes"),
            }
    provenance: dict[str, Any] = {
        "snapshot_version": snapshot.get("version"),
        "manifest_path": str(snapshot.get("manifest_path") or ""),
        "inputs": inputs,
        "executables": executables,
    }
    runtime_identity = snapshot.get("runtime_identity")
    if isinstance(runtime_identity, Mapping):
        provenance["runtime_identity"] = copy.deepcopy(dict(runtime_identity))
    return provenance


def exact_artifact_envelope_for_entry(
    payload: Mapping[str, Any],
    *,
    entry: QueueEntry,
    engine: str,
    job_dir: Path,
    require_job_dir: bool,
    require_generation: bool = False,
) -> dict[str, Any]:
    """Return a schema-valid envelope naming this exact durable queue row."""
    if not payload or payload.get("schema_version") != 1:
        return {}
    if str(payload.get("engine") or "").strip().lower() != str(engine).strip().lower():
        return {}
    job = payload.get("job")
    if not isinstance(job, Mapping):
        return {}
    if str(job.get("id") or "").strip() != str(entry.task_id).strip():
        return {}
    if str(job.get("task_id") or "").strip() != str(entry.task_id).strip():
        return {}
    if str(job.get("queue_id") or "").strip() != str(entry.queue_id).strip():
        return {}
    if str(job.get("app_name") or "").strip() != str(entry.app_name).strip():
        return {}
    expected_generation = queue_entry_generation_token(entry)
    artifact_generation = str(job.get("generation") or "").strip()
    if artifact_generation:
        if artifact_generation != expected_generation:
            return {}
    elif require_generation:
        return {}
    raw_job_dir = str(job.get("dir") or "").strip()
    if require_job_dir and not raw_job_dir:
        return {}
    if raw_job_dir:
        try:
            if Path(raw_job_dir).expanduser().resolve() != job_dir.expanduser().resolve():
                return {}
        except (OSError, RuntimeError):
            return {}
    return dict(payload)


def require_pending_cancel_state_for_entry(
    payload: Mapping[str, Any] | None,
    *,
    entry: QueueEntry,
    engine: str,
    job_dir: Path,
) -> dict[str, Any]:
    """Require queued or idempotently-cancelled state for this exact generation."""

    if payload is None:
        raise ValueError("Pending cancellation requires state for the exact queue generation")
    exact_state = exact_artifact_envelope_for_entry(
        payload,
        entry=entry,
        engine=engine,
        job_dir=job_dir,
        require_job_dir=True,
        require_generation=True,
    )
    if not exact_state:
        raise ValueError("Pending cancellation requires state for the exact queue generation")
    status = exact_state.get("status")
    state = str(status.get("state") or "").strip().lower() if isinstance(status, Mapping) else ""
    if state == "queued":
        return exact_state
    if state == "cancelled" and terminal_state_is_consistent(
        state=exact_state,
        entry=entry,
        engine=engine,
        job_dir=job_dir,
        expected_status="cancelled",
        expected_reason="cancel_requested",
    ):
        return exact_state
    raise ValueError("Pending cancellation requires queued state or an exact cancelled replay")


def canonical_terminal_state_payload(
    payload: Mapping[str, Any],
    *,
    job_dir: Path,
    status: str,
    reason: str,
    exit_code: int | None,
    generation: str,
    updated_at: str,
) -> dict[str, Any]:
    """Rebuild the authoritative terminal state for one exact queue generation."""

    canonical = copy.deepcopy(dict(payload))
    job = canonical.get("job")
    if not isinstance(job, dict):
        job = {}
        canonical["job"] = job
    job["dir"] = str(job_dir)
    job["generation"] = generation
    status_payload = canonical.get("status")
    if not isinstance(status_payload, dict):
        status_payload = {}
        canonical["status"] = status_payload
    status_payload.update(
        {
            "state": status,
            "reason": reason,
            "exit_code": exit_code,
        }
    )
    timestamps = canonical.get("timestamps")
    if not isinstance(timestamps, dict):
        timestamps = {}
        canonical["timestamps"] = timestamps
    timestamps.update({"updated_at": updated_at, "finished_at": updated_at})
    return canonical


def terminal_state_is_consistent(
    *,
    state: Mapping[str, Any],
    entry: QueueEntry,
    engine: str,
    job_dir: Path,
    expected_status: str,
    expected_reason: str,
) -> bool:
    """Check that state is exact-generation terminal evidence for the queue row."""

    exact_state = exact_artifact_envelope_for_entry(
        state,
        entry=entry,
        engine=engine,
        job_dir=job_dir,
        require_job_dir=True,
        require_generation=True,
    )
    if not exact_state:
        return False
    state_status = exact_state.get("status")
    if not isinstance(state_status, Mapping):
        return False
    normalized_expected = str(expected_status).strip().lower()
    if str(state_status.get("state") or "").strip().lower() != normalized_expected:
        return False
    exit_code = state_status.get("exit_code")
    reason = str(state_status.get("reason") or "").strip()
    normalized_reason = str(expected_reason).strip()
    if normalized_expected == "completed":
        return exit_code == 0 and reason == "completed"
    if normalized_expected == "failed":
        return bool(normalized_reason) and type(exit_code) is int and reason == normalized_reason
    if normalized_expected == "cancelled":
        return (exit_code is None or type(exit_code) is int) and reason == "cancel_requested"
    return False


def matching_terminal_state_for_entry(
    *,
    state: Mapping[str, Any],
    entry: QueueEntry,
    engine: str,
    job_dir: Path,
) -> dict[str, Any] | None:
    """Select terminal state only when it proves this exact queue generation."""

    expected_engine = str(engine).strip().lower()
    expected_job_dir = job_dir.expanduser().resolve()
    terminal_statuses = {"completed", "failed", "cancelled"}

    def terminal_status(payload: Mapping[str, Any]) -> str:
        status = payload.get("status")
        if not isinstance(status, Mapping):
            return ""
        state = status.get("state")
        if not isinstance(state, str):
            return ""
        normalized = state.strip().lower()
        return normalized if normalized in terminal_statuses else ""

    def trusted_datetime(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip())
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)

    def trusted_timestamp(payload: Mapping[str, Any]) -> datetime | None:
        timestamps = payload.get("timestamps")
        if not isinstance(timestamps, Mapping):
            return None
        for key in ("updated_at", "finished_at"):
            parsed = trusted_datetime(timestamps.get(key))
            if parsed is not None:
                return parsed
        return None

    exact_state = exact_artifact_envelope_for_entry(
        state,
        entry=entry,
        engine=expected_engine,
        job_dir=expected_job_dir,
        require_job_dir=True,
        require_generation=True,
    )
    if not exact_state:
        return None
    state_status = terminal_status(exact_state)
    state_status_payload = exact_state.get("status")
    state_reason = (
        str(state_status_payload.get("reason") or "").strip()
        if isinstance(state_status_payload, Mapping)
        else ""
    )
    if not state_status or not terminal_state_is_consistent(
        state=exact_state,
        entry=entry,
        engine=expected_engine,
        job_dir=expected_job_dir,
        expected_status=state_status,
        expected_reason=state_reason,
    ):
        return None
    terminal_timestamp = trusted_timestamp(exact_state)
    claim_started_at = trusted_datetime(getattr(entry, "started_at", ""))
    if (
        terminal_timestamp is None
        or claim_started_at is None
        or terminal_timestamp < claim_started_at
    ):
        # Every terminal envelope must be demonstrably newer than this queue
        # claim. Otherwise state/report may both belong to an earlier attempt
        # that was left in the reused job directory before the current claim.
        return None
    return exact_state


def is_resumed_state(
    previous_state: dict[str, Any],
    *,
    is_recovery_pending_fn: Callable[[dict[str, Any]], bool],
) -> bool:
    status_payload = previous_state.get("status")
    status_text = (
        str(status_payload.get("state", "")).strip().lower()
        if isinstance(status_payload, Mapping)
        else str(status_payload or "").strip().lower()
    )
    return is_recovery_pending_fn(previous_state) or status_text == "running"


def default_engine_resource_caps(cfg: Any) -> dict[str, int]:
    from orca_auto.core.indexing.engines import resource_dict

    from ..resource_requests import engine_resource_caps

    return engine_resource_caps(cfg, resource_dict_fn=resource_dict)


def default_entry_resource_request(cfg: Any, entry: Any) -> dict[str, int]:
    from ..resource_requests import entry_resource_request

    return entry_resource_request(
        cfg,
        entry,
        resource_caps_fn=default_engine_resource_caps,
    )


def build_running_state_payload(
    entry: QueueEntry,
    *,
    job_dir: Path,
    selected_input_xyz: str,
    started_at: str,
    updated_at: str,
    previous_state: dict[str, Any] | None,
    resumed: bool,
    resource_request: dict[str, int],
    engine_fields: dict[str, Any] | None = None,
    detail_fields: dict[str, Any] | None = None,
    engine: str = "",
    worker_job_pid: int | None = None,
) -> dict[str, Any]:
    recovery_reason = _queue_execution.recovery_reason(previous_state)
    engine_payload = {
        **dict(engine_fields or {}),
        **dict(detail_fields or {}),
    }
    return build_engine_artifact_payload(
        engine=engine,
        job=EngineArtifactJob(
            id=entry.task_id,
            queue_id=entry.queue_id,
            dir=str(job_dir),
            app_name=entry.app_name,
            task_id=entry.task_id,
            generation=queue_entry_generation_token(entry),
        ),
        status=EngineArtifactStatus(
            state="running",
            reason=recovery_reason if resumed else "",
        ),
        input=NormalizedArtifactInput(
            primary_path=selected_input_xyz,
            selected_xyz_path=selected_input_xyz,
        ),
        resources=EngineArtifactResources(
            request=resource_request,
            actual=dict(resource_request),
        ),
        timestamps=EngineArtifactTimestamps(
            created_at=_queue_execution.created_at(previous_state) or started_at,
            started_at=started_at,
            updated_at=updated_at,
        ),
        recovery=EngineArtifactRecovery(
            pending=False,
            reason=recovery_reason,
            count=_queue_execution.recovery_count(previous_state),
            resumed=bool(resumed),
        ),
        process=EngineArtifactProcess(worker_pid=worker_job_pid),
        artifacts={},
        engine_payload=engine_payload,
    )


def write_running_state_artifact(
    entry: QueueEntry,
    *,
    job_dir_text: str,
    selected_input_xyz: str,
    started_at: str,
    updated_at: str,
    previous_state: dict[str, Any] | None,
    resumed: bool,
    resource_request: dict[str, int],
    write_state_fn: Callable[..., Any],
    engine: str = "",
    engine_fields: dict[str, Any] | None = None,
    detail_fields: dict[str, Any] | None = None,
    worker_job_pid: int | None = None,
) -> None:
    if not job_dir_text:
        return
    job_dir = Path(job_dir_text).expanduser().resolve()
    payload = build_running_state_payload(
        entry,
        job_dir=job_dir,
        selected_input_xyz=selected_input_xyz,
        started_at=started_at,
        updated_at=updated_at,
        previous_state=previous_state,
        resumed=resumed,
        resource_request=resource_request,
        engine_fields=engine_fields,
        detail_fields=detail_fields,
        engine=engine,
        worker_job_pid=worker_job_pid,
    )
    write_state_fn(job_dir, payload)


def write_running_engine_state_artifact(
    entry: QueueEntry,
    *,
    job_dir_text: str,
    started_at: str,
    updated_at: str,
    previous_state: dict[str, Any] | None,
    resumed: bool,
    resource_request: dict[str, int],
    artifact_fields: EngineArtifactFields,
    write_state_fn: Callable[..., Any],
    worker_job_pid: int | None = None,
) -> None:
    write_running_state_artifact(
        entry,
        job_dir_text=job_dir_text,
        selected_input_xyz=artifact_fields.selected_input_xyz,
        started_at=started_at,
        updated_at=updated_at,
        previous_state=previous_state,
        resumed=resumed,
        resource_request=resource_request,
        write_state_fn=write_state_fn,
        engine=artifact_fields.engine,
        engine_fields=artifact_fields.engine_payload(),
        detail_fields=artifact_fields.detail_payload(),
        worker_job_pid=worker_job_pid,
    )


def build_terminal_state_payload(
    entry: QueueEntry,
    result: Any,
    *,
    job_dir_text: str,
    selected_input_xyz: str,
    previous_state: dict[str, Any] | None,
    resumed: bool,
    engine_fields: dict[str, Any] | None = None,
    detail_fields: dict[str, Any] | None = None,
    engine: str = "",
) -> dict[str, Any]:
    recovery_reason = _queue_execution.recovery_reason(previous_state)
    engine_payload = {
        **dict(engine_fields or {}),
        **dict(detail_fields or {}),
    }
    return build_engine_artifact_payload(
        engine=engine,
        job=EngineArtifactJob(
            id=entry.task_id,
            queue_id=entry.queue_id,
            dir=job_dir_text,
            app_name=entry.app_name,
            task_id=entry.task_id,
            generation=queue_entry_generation_token(entry),
        ),
        status=EngineArtifactStatus(
            state=result.status,
            reason=result.reason,
            exit_code=result.exit_code,
        ),
        input=NormalizedArtifactInput(
            primary_path=selected_input_xyz,
            selected_xyz_path=selected_input_xyz,
        ),
        resources=EngineArtifactResources(
            request=dict(result.resource_request),
            actual=dict(result.resource_actual),
        ),
        timestamps=EngineArtifactTimestamps(
            created_at=_queue_execution.created_at(previous_state),
            started_at=result.started_at,
            updated_at=result.finished_at,
            finished_at=result.finished_at,
        ),
        recovery=EngineArtifactRecovery(
            pending=False,
            reason=recovery_reason,
            count=_queue_execution.recovery_count(previous_state),
            resumed=bool(resumed),
        ),
        process=EngineArtifactProcess(),
        artifacts={
            "manifest_path": result.manifest_path,
            "stdout_log": result.stdout_log,
            "stderr_log": result.stderr_log,
        },
        engine_payload=engine_payload,
    )


def write_terminal_engine_artifacts(
    entry: QueueEntry,
    result: Any,
    *,
    job_dir_text: str,
    previous_state: dict[str, Any] | None,
    resumed: bool,
    artifact_fields: EngineArtifactFields,
    write_state_fn: Callable[..., Any],
) -> None:
    if not job_dir_text:
        return
    state = build_terminal_state_payload(
        entry,
        result,
        job_dir_text=job_dir_text,
        selected_input_xyz=artifact_fields.selected_input_xyz,
        previous_state=previous_state,
        resumed=resumed,
        engine=artifact_fields.engine,
        engine_fields=artifact_fields.engine_payload(),
        detail_fields=artifact_fields.detail_payload(),
    )
    engine_payload = state.get("engine_payload")
    if not isinstance(engine_payload, dict):
        engine_payload = {}
        state["engine_payload"] = engine_payload
    engine_payload["command"] = list(result.command)
    write_state_fn(Path(job_dir_text).expanduser().resolve(), state)


def build_terminal_result(
    result_cls: Callable[..., TerminalResultT],
    entry: QueueEntry,
    *,
    job_dir: Path,
    selected_xyz: Path,
    log_prefix: str,
    manifest_filename: str,
    resource_request: dict[str, int],
    status: str,
    reason: str,
    now_utc_iso_fn: Callable[[], str],
    command: tuple[str, ...] = (),
    exit_code: int = 1,
    engine_fields: dict[str, Any] | None = None,
    detail_fields: dict[str, Any] | None = None,
) -> TerminalResultT:
    terminal_time = now_utc_iso_fn()
    manifest_path = (job_dir / manifest_filename).resolve()
    return result_cls(
        status=status,
        reason=reason,
        command=command,
        exit_code=exit_code,
        started_at=entry.started_at or terminal_time,
        finished_at=terminal_time,
        stdout_log=str((job_dir / f"{log_prefix}.stdout.log").resolve()),
        stderr_log=str((job_dir / f"{log_prefix}.stderr.log").resolve()),
        selected_input_xyz=str(selected_xyz.resolve()),
        **dict(engine_fields or {}),
        **dict(detail_fields or {}),
        manifest_path=str(manifest_path) if manifest_path.exists() else "",
        resource_request=resource_request,
        resource_actual=dict(resource_request),
    )


def build_terminal_result_from_context(
    build_terminal_result_fn: Callable[..., TerminalResultT],
    context: Any,
    *,
    identity_fields: Mapping[str, Any],
    status: str,
    reason: str,
    exit_code: int = 1,
    now_utc_iso: str | None = None,
) -> TerminalResultT:
    kwargs: dict[str, Any] = {
        "job_dir": context.job_dir,
        "selected_xyz": context.selected_xyz,
        "resource_request": context.resource_request,
        "status": status,
        "reason": reason,
        "exit_code": exit_code,
        **dict(identity_fields),
    }
    if now_utc_iso is not None:
        kwargs["now_utc_iso_fn"] = lambda: now_utc_iso
    return build_terminal_result_fn(context.entry, **kwargs)


__all__ = [
    "EngineArtifactFields",
    "canonical_terminal_state_payload",
    "build_running_state_payload",
    "build_terminal_result",
    "build_terminal_result_from_context",
    "build_terminal_state_payload",
    "default_engine_resource_caps",
    "default_entry_resource_request",
    "is_resumed_state",
    "require_pending_cancel_state_for_entry",
    "terminal_state_is_consistent",
    "write_running_engine_state_artifact",
    "write_running_state_artifact",
    "write_terminal_engine_artifacts",
]
