from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.config.files import load_bounded_yaml_data
from orca_auto.core.paths import validate_job_dir
from orca_auto.core.paths.reserved import relative_reaches_reserved_generation
from orca_auto.core.paths.workflow import workflow_workspace_internal_engine_paths_from_path
from orca_auto.core.queue.generation import (
    queue_entry_generation_token,
)
from orca_auto.core.queue.priority import normalize_queue_priority

SUPPRESS_QUEUED_NOTIFICATION_CONTEXT_KEY = "suppress_queued_notification"
RunDirPublicationGuard = Callable[[str], None]
_ACTIVE_RUN_DIR_PUBLICATION_GUARD: ContextVar[RunDirPublicationGuard | None] = ContextVar(
    "orca_auto_run_dir_publication_guard",
    default=None,
)
_ACTIVE_RUN_DIR_PINNED_TARGET: ContextVar[Path | None] = ContextVar(
    "orca_auto_run_dir_pinned_target",
    default=None,
)


@contextmanager
def use_run_dir_publication_guard(
    guard: RunDirPublicationGuard,
    *,
    pinned_target: str | Path | None = None,
) -> Iterator[None]:
    guard_token = _ACTIVE_RUN_DIR_PUBLICATION_GUARD.set(guard)
    target_token = _ACTIVE_RUN_DIR_PINNED_TARGET.set(
        Path(pinned_target) if pinned_target is not None else None
    )
    try:
        yield
    finally:
        _ACTIVE_RUN_DIR_PINNED_TARGET.reset(target_token)
        _ACTIVE_RUN_DIR_PUBLICATION_GUARD.reset(guard_token)


def assert_run_dir_publication_allowed(stage: str) -> None:
    guard = _ACTIVE_RUN_DIR_PUBLICATION_GUARD.get()
    if guard is not None:
        guard(stage)


def active_run_dir_pinned_target() -> Path | None:
    """Return the fd-backed public target while synchronous submission is active."""

    return _ACTIVE_RUN_DIR_PINNED_TARGET.get()


def validate_production_run_dir_target(
    raw_job_dir: str | Path,
    runs_root: str | Path,
) -> None:
    """Reject public submission of a nested ORCA execution generation."""

    resolved_root = Path(runs_root).expanduser().resolve()
    try:
        relative = Path(raw_job_dir).expanduser().resolve().relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return
    # Workflow workspaces share the generation name shape but are valid
    # run-dir targets (restart); ORCA execution generations are not.
    if relative_reaches_reserved_generation(resolved_root, relative):
        raise ValueError(
            "run-dir target is inside an ORCA execution generation; submit its public parent "
            f"job directory instead: {raw_job_dir}"
        )


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
        priority=normalize_queue_priority(getattr(args, "priority", 10)),
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
    module_name: str = __name__,
) -> Callable[[Any, EngineRunDirSubmission, Any], bool]:
    def record_queued(cfg: Any, submission: EngineRunDirSubmission, entry: Any) -> bool:
        return record_engine_run_dir_queued_with_callbacks(
            cfg,
            submission,
            entry,
            callbacks=callbacks,
        )

    record_queued.__name__ = "_record_queued"
    record_queued.__qualname__ = "_record_queued"
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

    parsed = load_bounded_yaml_data(path) or {}
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
    state_job = record.state_payload.get("job")
    if not isinstance(state_job, dict):
        raise ValueError("queued engine state is missing its job identity")
    state_job.update(
        {
            "queue_id": str(entry.queue_id),
            "app_name": str(entry.app_name),
            "task_id": str(entry.task_id),
            "generation": queue_entry_generation_token(entry),
        }
    )
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
