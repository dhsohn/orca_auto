from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from orca_auto.core.paths import validate_job_dir
from orca_auto.core.paths.workflow import workflow_workspace_internal_engine_paths_from_path

SUPPRESS_QUEUED_NOTIFICATION_CONTEXT_KEY = "suppress_queued_notification"


@dataclass(frozen=True)
class EngineRunDirSubmission:
    queue_root: Path
    app_name: str
    task_id: str
    task_kind: str
    engine: str
    priority: int
    metadata: dict[str, Any]
    context: dict[str, Any]


@dataclass(frozen=True)
class EngineSubmissionSpec:
    queue_root: Path
    app_name: str
    task_id: str
    task_kind: str
    engine: str
    metadata: Mapping[str, Any]
    context: Mapping[str, Any]


@dataclass(frozen=True)
class EngineQueuedRecord:
    state_payload: dict[str, Any]
    index_fields: dict[str, Any]
    notification_fields: dict[str, Any]


@dataclass(frozen=True)
class EngineQueuedRecordCallbacks:
    build_record: Callable[[EngineRunDirSubmission, Any], EngineQueuedRecord]
    write_state: Callable[[Path, dict[str, Any]], Any]
    upsert_job_record: Callable[..., Any]
    notify_job_queued: Callable[..., Any]


def engine_resource_fields(resource_request: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    payload = dict(resource_request or {})
    return {
        "resource_request": payload,
        "resource_actual": dict(payload),
    }


def manifest_present_text(manifest: dict[str, Any]) -> str:
    return "true" if manifest else "false"


def build_engine_run_dir_submission(
    *,
    queue_root: Path,
    app_name: str,
    task_id: str,
    task_kind: str,
    engine: str,
    args: Any,
    metadata: dict[str, Any],
    context: dict[str, Any],
) -> EngineRunDirSubmission:
    return EngineRunDirSubmission(
        queue_root=queue_root,
        app_name=app_name,
        task_id=task_id,
        task_kind=task_kind,
        engine=engine,
        priority=int(getattr(args, "priority", 10)),
        metadata=metadata,
        context=context,
    )


def build_engine_run_dir_submission_from_spec(
    *,
    spec: EngineSubmissionSpec,
    args: Any,
    manifest: dict[str, Any],
    resource_request: dict[str, Any] | None,
) -> EngineRunDirSubmission:
    resource_fields = engine_resource_fields(resource_request)
    metadata = dict(spec.metadata)
    metadata["manifest_present"] = manifest_present_text(manifest)
    metadata.update(resource_fields)
    context = dict(spec.context)
    context["resource_request"] = resource_fields["resource_request"]
    return build_engine_run_dir_submission(
        queue_root=spec.queue_root,
        app_name=spec.app_name,
        task_id=spec.task_id,
        task_kind=spec.task_kind,
        engine=spec.engine,
        args=args,
        metadata=metadata,
        context=context,
    )


def build_engine_queued_record(
    *,
    submission: EngineRunDirSubmission,
    state_payload: dict[str, Any],
    index_fields: dict[str, Any],
    notification_fields: dict[str, Any],
) -> EngineQueuedRecord:
    resource_request = submission.metadata.get("resource_request")
    if not isinstance(resource_request, dict):
        resource_request = submission.context["resource_request"]
    index_payload = dict(index_fields)
    index_payload["resource_request"] = resource_request
    index_payload["resource_actual"] = resource_request
    return EngineQueuedRecord(
        state_payload=dict(state_payload),
        index_fields=index_payload,
        notification_fields=dict(notification_fields),
    )


def record_engine_run_dir_queued_with_callbacks(
    cfg: Any,
    submission: EngineRunDirSubmission,
    entry: Any,
    *,
    callbacks: EngineQueuedRecordCallbacks,
) -> bool:
    return record_queued_common(
        cfg,
        submission,
        entry,
        build_record_fn=callbacks.build_record,
        write_state_fn=callbacks.write_state,
        upsert_job_record_fn=callbacks.upsert_job_record,
        notify_job_queued_fn=callbacks.notify_job_queued,
    )


def engine_run_dir_queued_recorder_from_callbacks(
    callbacks: EngineQueuedRecordCallbacks,
    *,
    recorder_name: str = "_record_queued",
    module_name: str = __name__,
) -> Callable[[Any, EngineRunDirSubmission, Any], bool]:
    def record_queued(cfg: Any, submission: EngineRunDirSubmission, entry: Any) -> bool:
        return record_engine_run_dir_queued_with_callbacks(
            cfg,
            submission,
            entry,
            callbacks=callbacks,
        )

    record_queued.__name__ = recorder_name
    record_queued.__qualname__ = recorder_name
    record_queued.__module__ = module_name
    return record_queued


def load_yaml_job_manifest(
    job_dir: Path,
    filename: str,
    *,
    missing_message: str | None = None,
    invalid_message: str,
) -> dict[str, Any]:
    path = job_dir / filename
    if not path.exists():
        if missing_message is None:
            return {}
        raise ValueError(missing_message.format(path=path))

    with path.open("r", encoding="utf-8") as handle:
        parsed = yaml.safe_load(handle) or {}
    if not isinstance(parsed, dict):
        raise ValueError(invalid_message.format(path=path))
    return parsed


def resolve_engine_job_dir(
    cfg: Any,
    raw_job_dir: str,
    *,
    engine: str,
    workflow_error_message: str,
    validate_job_dir_fn: Any = validate_job_dir,
    workflow_paths_from_path_fn: Any = workflow_workspace_internal_engine_paths_from_path,
) -> Path:
    candidate = Path(raw_job_dir).expanduser().resolve()
    workflow_root = str(getattr(cfg, "workflow_root", "")).strip()
    if workflow_root:
        runtime_paths = workflow_paths_from_path_fn(
            candidate,
            workflow_root=workflow_root,
            engine=engine,
        )
        if runtime_paths is None:
            raise ValueError(workflow_error_message)
        return validate_job_dir_fn(
            raw_job_dir,
            str(runtime_paths["allowed_root"]),
            label="Job directory",
        )
    return validate_job_dir_fn(raw_job_dir, cfg.runtime.allowed_root, label="Job directory")


def record_queued_common(
    cfg: Any,
    submission: EngineRunDirSubmission,
    entry: Any,
    *,
    build_record_fn: Callable[[EngineRunDirSubmission, Any], EngineQueuedRecord],
    write_state_fn: Callable[[Path, dict[str, Any]], Any],
    upsert_job_record_fn: Callable[..., Any],
    notify_job_queued_fn: Callable[..., Any],
) -> bool:
    """Write the durable queued view and attempt its notification at most once.

    A replay deliberately suppresses the notification because a raised transport
    error cannot distinguish pre-delivery failure from post-delivery failure.
    Exactly-once delivery therefore requires a separate durable outbox with a
    transport idempotency key; it is not part of this queue publication protocol.
    """
    raw_job_dir = submission.metadata.get("job_dir") or submission.context["job_dir"]
    job_dir = Path(raw_job_dir).expanduser().resolve()
    record = build_record_fn(submission, entry)
    write_state_fn(job_dir, record.state_payload)
    upsert_job_record_fn(
        cfg,
        job_id=submission.task_id,
        status="queued",
        job_dir=job_dir,
        **record.index_fields,
    )
    if bool(submission.context.get(SUPPRESS_QUEUED_NOTIFICATION_CONTEXT_KEY, False)):
        return True
    return bool(
        notify_job_queued_fn(
            cfg,
            job_id=submission.task_id,
            queue_id=entry.queue_id,
            job_dir=job_dir,
            **record.notification_fields,
        )
    )
