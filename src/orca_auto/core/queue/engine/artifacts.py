from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
            detail_fields=detail_fields,
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
