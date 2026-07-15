from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    build_engine_report_markdown,
)
from orca_auto.core.queue import execution as _queue_execution
from orca_auto.core.queue.generation import queue_entry_generation_token

from ..types import QueueEntry


@dataclass(frozen=True)
class TerminalArtifactPayloads:
    state: dict[str, Any]
    report: dict[str, Any]


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


@dataclass(frozen=True)
class TerminalArtifactWriters:
    write_state: Callable[..., Any]
    write_report_json: Callable[..., Any]
    write_report_md_lines: Callable[..., Any]


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


def canonical_terminal_artifact_payloads(
    payload: Mapping[str, Any],
    *,
    job_dir: Path,
    status: str,
    reason: str,
    exit_code: int | None,
    generation: str,
    updated_at: str,
) -> TerminalArtifactPayloads:
    """Rebuild both terminal envelopes from one exact-generation source."""

    def canonical_payload(*, state: bool) -> dict[str, Any]:
        canonical = copy.deepcopy(dict(payload))
        job = canonical.get("job")
        if not isinstance(job, dict):
            job = {}
            canonical["job"] = job
        job["dir"] = str(job_dir) if state else ""
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

    return TerminalArtifactPayloads(
        state=canonical_payload(state=True),
        report=canonical_payload(state=False),
    )


def terminal_artifact_pair_is_consistent(
    *,
    state: Mapping[str, Any],
    report: Mapping[str, Any],
    entry: QueueEntry,
    engine: str,
    job_dir: Path,
    expected_status: str,
    expected_reason: str,
) -> bool:
    """Check that both envelopes are exact, terminal, and semantically equal."""

    exact_state = exact_artifact_envelope_for_entry(
        state,
        entry=entry,
        engine=engine,
        job_dir=job_dir,
        require_job_dir=True,
        require_generation=True,
    )
    exact_report = exact_artifact_envelope_for_entry(
        report,
        entry=entry,
        engine=engine,
        job_dir=job_dir,
        require_job_dir=False,
        require_generation=True,
    )
    if not exact_state or not exact_report:
        return False

    def normalized(payload: Mapping[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(dict(payload))
        job = value.get("job")
        if isinstance(job, dict):
            job["dir"] = ""
        engine_payload = value.get("engine_payload")
        if isinstance(engine_payload, dict):
            engine_payload.pop("command", None)
        return value

    state_status = exact_state.get("status")
    report_status = exact_report.get("status")
    if not isinstance(state_status, Mapping) or not isinstance(report_status, Mapping):
        return False
    normalized_expected = str(expected_status).strip().lower()
    if (
        str(state_status.get("state") or "").strip().lower() != normalized_expected
        or str(report_status.get("state") or "").strip().lower() != normalized_expected
    ):
        return False
    exit_code = state_status.get("exit_code")
    reason = str(state_status.get("reason") or "").strip()
    normalized_reason = str(expected_reason).strip()
    if normalized_expected == "completed" and (exit_code != 0 or reason != "completed"):
        return False
    if normalized_expected == "failed" and type(exit_code) is not int:
        return False
    if normalized_expected == "cancelled" and (
        (exit_code is not None and type(exit_code) is not int) or reason != "cancel_requested"
    ):
        return False
    if normalized_expected == "failed" and reason != normalized_reason:
        return False
    return normalized(exact_state) == normalized(exact_report)


def matching_terminal_artifacts_for_entry(
    *,
    state: Mapping[str, Any],
    report: Mapping[str, Any],
    entry: QueueEntry,
    engine: str,
    job_dir: Path,
) -> TerminalArtifactPayloads | None:
    """Select only terminal envelopes that name this exact queue generation."""

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
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
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
    )
    if exact_state:
        state_status = terminal_status(exact_state)
        if not state_status:
            # Every execution writes its running state before it can write a
            # terminal report. Therefore an exact malformed or nonterminal
            # state is newer than (or makes ambiguous) a same-generation
            # terminal report left by an earlier attempt, and must fence it.
            return None
    matched_state = exact_state
    report_envelope = exact_artifact_envelope_for_entry(
        report,
        entry=entry,
        engine=expected_engine,
        job_dir=expected_job_dir,
        require_job_dir=False,
    )
    matched_report = report_envelope if terminal_status(report_envelope) else {}
    if matched_state and matched_report:
        state_status = terminal_status(matched_state)
        report_status = terminal_status(matched_report)
        if state_status != report_status:
            matched_report = {}
        else:
            state_timestamp = trusted_timestamp(matched_state)
            report_timestamp = trusted_timestamp(matched_report)
            if (
                state_timestamp is None
                or report_timestamp is None
                or report_timestamp <= state_timestamp
            ):
                state_status_payload = matched_state.get("status")
                expected_reason = (
                    str(state_status_payload.get("reason") or "").strip()
                    if isinstance(state_status_payload, Mapping)
                    else ""
                )
                if not terminal_artifact_pair_is_consistent(
                    state=matched_state,
                    report=matched_report,
                    entry=entry,
                    engine=expected_engine,
                    job_dir=expected_job_dir,
                    expected_status=state_status,
                    expected_reason=expected_reason,
                ):
                    # Retain a same-timestamp report only when the complete
                    # exact-generation pair agrees. This preserves report-only
                    # command provenance without letting an older report
                    # override state input/resource identity.
                    matched_report = {}
            else:
                matched_state = {}
    if not matched_state and not matched_report:
        return None
    selected_terminal = matched_report or matched_state
    terminal_timestamp = trusted_timestamp(selected_terminal)
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
    return TerminalArtifactPayloads(state=matched_state, report=matched_report)


def terminal_artifacts_match_entry(
    *,
    state: Mapping[str, Any],
    report: Mapping[str, Any],
    entry: QueueEntry,
    engine: str,
    job_dir: Path,
) -> bool:
    return (
        matching_terminal_artifacts_for_entry(
            state=state,
            report=report,
            entry=entry,
            engine=engine,
            job_dir=job_dir,
        )
        is not None
    )


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


def build_terminal_report_payload(
    entry: QueueEntry,
    result: Any,
    *,
    selected_input_xyz: str,
    previous_state: dict[str, Any] | None,
    resumed: bool,
    engine_fields: dict[str, Any] | None = None,
    detail_fields: dict[str, Any] | None = None,
    engine: str = "",
) -> dict[str, Any]:
    payload = build_terminal_state_payload(
        entry,
        result,
        job_dir_text="",
        selected_input_xyz=selected_input_xyz,
        previous_state=previous_state,
        resumed=resumed,
        engine_fields=engine_fields,
        detail_fields={
            **dict(detail_fields or {}),
            "command": list(result.command),
        },
        engine=engine,
    )
    payload["job"]["dir"] = ""
    return payload


def build_terminal_artifact_payloads(
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
) -> TerminalArtifactPayloads:
    return TerminalArtifactPayloads(
        state=build_terminal_state_payload(
            entry,
            result,
            job_dir_text=job_dir_text,
            selected_input_xyz=selected_input_xyz,
            previous_state=previous_state,
            resumed=resumed,
            engine_fields=engine_fields,
            detail_fields={
                **dict(detail_fields or {}),
                "command": list(result.command),
            },
            engine=engine,
        ),
        report=build_terminal_report_payload(
            entry,
            result,
            selected_input_xyz=selected_input_xyz,
            previous_state=previous_state,
            resumed=resumed,
            engine_fields=engine_fields,
            detail_fields=detail_fields,
            engine=engine,
        ),
    )


def write_terminal_execution_artifacts(
    entry: QueueEntry,
    result: Any,
    *,
    job_dir_text: str,
    selected_input_xyz: str,
    previous_state: dict[str, Any] | None,
    resumed: bool,
    engine_fields: dict[str, Any] | None,
    detail_fields: dict[str, Any] | None,
    report_lines: list[str],
    write_state_fn: Callable[..., Any],
    write_report_json_fn: Callable[..., Any],
    write_report_md_lines_fn: Callable[..., Any],
) -> None:
    write_terminal_engine_artifacts(
        entry,
        result,
        job_dir_text=job_dir_text,
        previous_state=previous_state,
        resumed=resumed,
        artifact_fields=EngineArtifactFields(
            selected_input_xyz=selected_input_xyz,
            engine=str((engine_fields or {}).get("_engine", "")),
            engine_fields=engine_fields,
            detail_fields=detail_fields,
        ),
        report_lines=report_lines,
        writers=TerminalArtifactWriters(
            write_state=write_state_fn,
            write_report_json=write_report_json_fn,
            write_report_md_lines=write_report_md_lines_fn,
        ),
    )


def write_terminal_engine_artifacts(
    entry: QueueEntry,
    result: Any,
    *,
    job_dir_text: str,
    previous_state: dict[str, Any] | None,
    resumed: bool,
    artifact_fields: EngineArtifactFields,
    report_lines: list[str],
    writers: TerminalArtifactWriters,
) -> None:
    if not job_dir_text:
        return
    payloads = build_terminal_artifact_payloads(
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
    report_lines = build_engine_report_markdown(payloads.report)
    _queue_execution.write_result_artifacts(
        job_dir_text,
        state_payload=payloads.state,
        report_payload=payloads.report,
        report_lines=report_lines,
        write_state_fn=writers.write_state,
        write_report_json_fn=writers.write_report_json,
        write_report_md_lines_fn=writers.write_report_md_lines,
    )


def terminal_report_lines(
    entry: QueueEntry,
    result: Any,
    *,
    title: str,
    selected_input_label: str,
    selected_input_xyz: str,
    engine_lines: list[str] | None = None,
    detail_lines: list[str] | None = None,
) -> list[str]:
    return [
        f"# {title}",
        "",
        f"- Job ID: `{entry.task_id}`",
        f"- Queue ID: `{entry.queue_id}`",
        f"- Status: `{result.status}`",
        f"- Reason: `{result.reason}`",
        *list(engine_lines or []),
        f"- {selected_input_label}: `{Path(selected_input_xyz).name}`",
        f"- Exit Code: `{result.exit_code}`",
        *list(detail_lines or []),
        f"- Resource Request: `{result.resource_request}`",
        f"- Resource Actual: `{result.resource_actual}`",
        f"- Stdout Log: `{result.stdout_log}`",
        f"- Stderr Log: `{result.stderr_log}`",
    ]


def build_terminal_result(
    result_cls: type,
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
) -> Any:
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
    build_terminal_result_fn: Callable[..., Any],
    context: Any,
    *,
    identity_fields: Mapping[str, Any],
    status: str,
    reason: str,
    exit_code: int = 1,
    now_utc_iso: str | None = None,
) -> Any:
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
    "TerminalArtifactPayloads",
    "TerminalArtifactWriters",
    "build_running_state_payload",
    "build_terminal_artifact_payloads",
    "build_terminal_report_payload",
    "build_terminal_result",
    "build_terminal_result_from_context",
    "build_terminal_state_payload",
    "default_engine_resource_caps",
    "default_entry_resource_request",
    "is_resumed_state",
    "terminal_report_lines",
    "write_running_engine_state_artifact",
    "write_running_state_artifact",
    "write_terminal_engine_artifacts",
    "write_terminal_execution_artifacts",
]
