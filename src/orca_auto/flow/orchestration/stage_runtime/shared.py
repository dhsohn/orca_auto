from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.statuses import (
    STATUS_PLANNED,
    STATUS_QUEUED,
    STATUS_SUBMISSION_FAILED,
    STATUS_SUBMITTED,
    STATUS_UNKNOWN,
    STATUS_WAITING_FOR_SLOT,
    SUBMISSION_DEFERRED_STATUSES,
)
from orca_auto.core.utils import normalize_bool as _shared_normalize_bool
from orca_auto.flow.orchestration.dep_types import OrchestrationDeps
from orca_auto.flow.orchestration.deps import (
    orchestration_context as _orchestration_context,
)
from orca_auto.flow.orchestration.stage_views import WorkflowStageView, WorkflowTaskView
from orca_auto.flow.state import (
    workflow_stage_dirnames_for_engine,
    workflow_workspace_internal_engine_paths,
)

_LOGGER = logging.getLogger("orca_auto.flow.orchestration.stage_runtime.shared")


def _stage_id_for_log(stage: dict[str, Any] | None) -> str:
    if not isinstance(stage, dict):
        return ""
    value = stage.get("stage_id")
    return "" if value is None else str(value).strip()


@dataclass(frozen=True)
class EngineStageSyncContext:
    o: OrchestrationDeps
    stage: dict[str, Any]
    task: dict[str, Any]
    task_payload: dict[str, Any]
    stage_metadata: dict[str, Any]
    engine: str
    stage_view: WorkflowStageView
    task_view: WorkflowTaskView

    def should_submit(self, *, submit_ready: bool, config_path: str | None) -> bool:
        return (
            self.o.stages._normalize_text(self.task.get("status")) == STATUS_PLANNED
            and submit_ready
            and bool(self.o.stages._normalize_text(config_path))
        )

    def set_submission_result(self, submission: dict[str, Any]) -> None:
        self.task_view.set_submission_result(submission)

    def set_output_artifacts(self, artifacts: list[dict[str, Any]]) -> None:
        self.stage_view.set_output_artifacts(artifacts)


def _engine_stage_sync_context(
    stage: dict[str, Any],
    *,
    engine: str,
    deps: OrchestrationDeps | None = None,
) -> EngineStageSyncContext | None:
    o = _orchestration_context(deps)
    task = stage.get("task")
    if not isinstance(task, dict) or o.stages._normalize_text(task.get("engine")) != engine:
        return None
    stage_view = WorkflowStageView(stage)
    task_view = stage_view.task
    return EngineStageSyncContext(
        o=o,
        stage=stage,
        task=task,
        task_payload=o.stages._task_payload_dict(task),
        stage_metadata=o.stages._stage_metadata(stage),
        engine=engine,
        stage_view=stage_view,
        task_view=task_view,
    )


def _load_contract_or_none(
    load_contract: Callable[..., Any],
    *,
    engine: str,
    target: str,
    stage: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any | None:
    try:
        return load_contract(target=target, **kwargs)
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "Failed to load %s artifact contract for target %r; returning None (stage_id=%r)",
            engine,
            target,
            _stage_id_for_log(stage),
            exc_info=True,
        )
        return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, (bool, str)) or value is None:
        return _shared_normalize_bool(value)
    return bool(value)


def _submission_status(submission: dict[str, Any]) -> str:
    return str(submission.get("status", "")).strip().lower()


def _submission_is_deferred(submission: dict[str, Any]) -> bool:
    return _submission_status(submission) in SUBMISSION_DEFERRED_STATUSES


def _submission_deferred_reason(submission: dict[str, Any]) -> str:
    reason = str(submission.get("reason", "")).strip()
    if reason:
        return reason
    return _submission_status(submission) or STATUS_WAITING_FOR_SLOT


def _mark_submission_deferred(
    *,
    stage: dict[str, Any],
    task: dict[str, Any],
    stage_metadata: dict[str, Any],
    submission: dict[str, Any],
) -> None:
    del task
    WorkflowStageView(stage).set_status_pair(
        stage_status=STATUS_PLANNED,
        task_status=STATUS_PLANNED,
    )
    stage_metadata["submission_status"] = STATUS_WAITING_FOR_SLOT
    stage_metadata["submission_deferred_reason"] = _submission_deferred_reason(submission)
    stage_metadata["last_submission_attempt_at"] = str(submission.get("submitted_at", "")).strip()
    stage_metadata.pop("submitted_at", None)
    if not str(submission.get("queue_id", "")).strip():
        stage_metadata.pop("queue_id", None)


def _clear_submission_deferred_metadata(stage_metadata: dict[str, Any]) -> None:
    stage_metadata.pop("submission_deferred_reason", None)
    stage_metadata.pop("last_submission_attempt_at", None)


def _apply_submission_result(
    *,
    stage: dict[str, Any],
    task: dict[str, Any],
    stage_metadata: dict[str, Any],
    submission: dict[str, Any],
    deferred_metadata: dict[str, Any] | None = None,
    active_metadata: dict[str, Any] | None = None,
    metadata_fields: tuple[tuple[str, str], ...] = (),
) -> bool:
    if _submission_is_deferred(submission):
        _mark_submission_deferred(
            stage=stage,
            task=task,
            stage_metadata=stage_metadata,
            submission=submission,
        )
        stage_metadata.update(deferred_metadata or {})
        return False

    submitted = _submission_status(submission) == STATUS_SUBMITTED
    WorkflowStageView(stage).set_status_pair(
        stage_status=STATUS_QUEUED if submitted else STATUS_SUBMISSION_FAILED,
        task_status=STATUS_SUBMITTED if submitted else STATUS_SUBMISSION_FAILED,
    )
    for metadata_key, submission_key in metadata_fields:
        stage_metadata[metadata_key] = submission.get(submission_key, "")
    stage_metadata.update(active_metadata or {})
    _clear_submission_deferred_metadata(stage_metadata)
    return True


def _apply_contract_status(stage: dict[str, Any], task: dict[str, Any], status: str) -> None:
    del task
    if status != STATUS_UNKNOWN:
        WorkflowStageView(stage).set_status_pair(stage_status=status, task_status=status)


def _engine_job_dir_contract_lookup(
    o: Any,
    stage: dict[str, Any],
    task_payload: dict[str, Any],
    *,
    runtime_paths: dict[str, Path],
    config_path: str | None,
    engine: str,
) -> tuple[str, Path] | None:
    job_dir_target = o.stages._normalize_text(task_payload.get("job_dir"))
    index_root = (
        runtime_paths["allowed_root"]
        or o.stages._load_config_root(config_path, engine=engine)
        or Path(job_dir_target or ".").resolve().parent
    )
    target = job_dir_target or o.stages._submission_target(stage)
    if not target:
        return None
    return target, index_root


def _workflow_internal_runs_root(path_text: str, *, engine: str) -> Path | None:
    text = str(path_text).strip()
    if not text:
        return None
    try:
        path = Path(text).expanduser().resolve()
    except OSError:
        return None

    engine_text = str(engine).strip().lower()
    stage_dirnames = workflow_stage_dirnames_for_engine(engine_text)
    for candidate in (path, *path.parents):
        if candidate.name in stage_dirnames:
            return candidate
    return None


def _workflow_internal_organized_root(path_text: str, *, engine: str) -> Path | None:
    runs_root = _workflow_internal_runs_root(path_text, engine=engine)
    if runs_root is None:
        return None
    try:
        if runs_root.name not in workflow_stage_dirnames_for_engine(engine):
            return None
        workspace_dir = runs_root.parent
        return workflow_workspace_internal_engine_paths(
            workspace_dir,
            engine=engine,
            stage_dirname=runs_root.name,
        )["organized_root"]
    except (IndexError, ValueError):
        return None


def _manifest_override_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if str(key).strip()}


def append_unique_artifact_impl(
    rows: list[dict[str, Any]],
    *,
    kind: str,
    path: str,
    selected: bool = False,
    metadata: dict[str, Any] | None = None,
    deps: OrchestrationDeps | None = None,
) -> None:
    o = _orchestration_context(deps)
    path_text = o.stages._normalize_text(path)
    if not path_text:
        return
    key = (o.stages._normalize_text(kind), path_text)
    seen = {
        (o.stages._normalize_text(item.get("kind")), o.stages._normalize_text(item.get("path")))
        for item in rows
        if isinstance(item, dict)
    }
    if key in seen:
        return
    rows.append(
        {
            "kind": o.stages._normalize_text(kind) or "artifact",
            "path": path_text,
            "selected": bool(selected),
            "metadata": dict(metadata or {}),
        }
    )
