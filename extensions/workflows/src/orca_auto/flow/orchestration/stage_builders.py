from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from orca_auto.flow.contracts import (
    WorkflowArtifactRef,
    WorkflowStage,
    WorkflowStageWithTaskPayload,
    WorkflowTask,
)

_CREST_RUN_DIR_API_NAME = "orca_auto.flow.engines.crest.submission.direct_enqueue"


@dataclass(frozen=True)
class _StagePayloadSections:
    task_payload: dict[str, Any]
    task_metadata: dict[str, Any]
    stage_metadata: dict[str, Any]


@dataclass(frozen=True)
class _EngineStageSpec:
    engine: str
    task_kind: str
    stage_kind: str
    submitter: str
    app_name: str
    submit_api_name: str
    config_placeholder: str


@dataclass(frozen=True)
class _EngineStageBuildRequest:
    workflow_id: str
    stage_id: str
    spec: _EngineStageSpec
    priority: int
    max_cores: int
    max_memory_gb: int
    sections: _StagePayloadSections
    input_artifacts: tuple[WorkflowArtifactRef, ...]
    enqueue_extra: dict[str, Any] | None = None


def _positive_resource_value(value: int, *, field_name: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise ValueError(f"{field_name} must be >= 1. got={parsed}")
    return parsed


def _resource_request(max_cores: int, max_memory_gb: int) -> dict[str, int]:
    return {
        "max_cores": _positive_resource_value(max_cores, field_name="max_cores"),
        "max_memory_gb": _positive_resource_value(
            max_memory_gb,
            field_name="max_memory_gb",
        ),
    }


def _stage_payload_sections(
    *,
    task_payload: dict[str, Any],
    task_metadata: dict[str, Any],
    stage_metadata: dict[str, Any],
    manifest_overrides: dict[str, Any] | None,
) -> _StagePayloadSections:
    resolved_overrides = dict(manifest_overrides or {})
    if resolved_overrides:
        task_payload["job_manifest_overrides"] = resolved_overrides
        task_metadata["job_manifest_overrides"] = resolved_overrides
        stage_metadata["job_manifest_overrides"] = resolved_overrides
    return _StagePayloadSections(
        task_payload=task_payload,
        task_metadata=task_metadata,
        stage_metadata=stage_metadata,
    )


def _engine_enqueue_payload(
    *,
    submitter: str,
    app_name: str,
    submit_api_name: str,
    config_placeholder: str,
    priority: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    command_argv = [
        submit_api_name,
        f"config={config_placeholder}",
        "job_dir=<job_dir>",
        f"priority={int(priority)}",
    ]
    payload: dict[str, Any] = {
        "submitter": submitter,
        "app_name": app_name,
        "command": " ".join(command_argv),
        "command_argv": command_argv,
        "requires_config": True,
        "config_argument_placeholder": config_placeholder,
        "job_dir": "",
        "priority": int(priority),
    }
    payload.update(extra or {})
    return payload


def _workflow_task(
    *,
    workflow_id: str,
    stage_id: str,
    engine: str,
    task_kind: str,
    max_cores: int,
    max_memory_gb: int,
    task_payload: dict[str, Any],
    task_metadata: dict[str, Any],
    submitter: str,
    app_name: str,
    submit_api_name: str,
    config_placeholder: str,
    priority: int,
    enqueue_extra: dict[str, Any] | None = None,
) -> WorkflowTask:
    return WorkflowTask.from_raw(
        task_id=f"{workflow_id}:{stage_id}",
        engine=engine,
        task_kind=task_kind,
        resource_request=_resource_request(max_cores, max_memory_gb),
        payload=task_payload,
        enqueue_payload=_engine_enqueue_payload(
            submitter=submitter,
            app_name=app_name,
            submit_api_name=submit_api_name,
            config_placeholder=config_placeholder,
            priority=priority,
            extra=enqueue_extra,
        ),
        metadata=task_metadata,
    )


def _planned_stage_payload(
    *,
    stage_id: str,
    stage_kind: str,
    input_artifacts: tuple[WorkflowArtifactRef, ...],
    task: WorkflowTask,
    metadata: dict[str, Any],
) -> WorkflowStageWithTaskPayload:
    stage = WorkflowStage(
        stage_id=stage_id,
        stage_kind=stage_kind,
        status="planned",
        input_artifacts=input_artifacts,
        output_artifacts=(),
        task=task,
        metadata=metadata,
    )
    return cast(WorkflowStageWithTaskPayload, stage.to_dict())


def _planned_engine_stage_payload(
    request: _EngineStageBuildRequest,
) -> WorkflowStageWithTaskPayload:
    task = _workflow_task(
        workflow_id=request.workflow_id,
        stage_id=request.stage_id,
        engine=request.spec.engine,
        task_kind=request.spec.task_kind,
        max_cores=request.max_cores,
        max_memory_gb=request.max_memory_gb,
        task_payload=request.sections.task_payload,
        task_metadata=request.sections.task_metadata,
        submitter=request.spec.submitter,
        app_name=request.spec.app_name,
        submit_api_name=request.spec.submit_api_name,
        config_placeholder=request.spec.config_placeholder,
        priority=request.priority,
        enqueue_extra=request.enqueue_extra,
    )
    return _planned_stage_payload(
        stage_id=request.stage_id,
        stage_kind=request.spec.stage_kind,
        input_artifacts=request.input_artifacts,
        task=task,
        metadata=request.sections.stage_metadata,
    )


_CREST_STAGE_SPEC = _EngineStageSpec(
    engine="crest",
    task_kind="conformer_search",
    stage_kind="crest_stage",
    submitter="orca_auto_crest",
    app_name="orca_auto_crest",
    submit_api_name=_CREST_RUN_DIR_API_NAME,
    config_placeholder="<crest_config>",
)


def new_crest_stage_impl(
    *,
    workflow_id: str,
    template_name: str,
    stage_id: str,
    source_path: str,
    input_role: str,
    mode: str,
    priority: int,
    max_cores: int,
    max_memory_gb: int,
    manifest_overrides: dict[str, Any] | None = None,
) -> WorkflowStageWithTaskPayload:
    sections = _stage_payload_sections(
        task_payload={
            "workflow_id": workflow_id,
            "template_name": template_name,
            "source_input_xyz": source_path,
            "selected_input_xyz": "",
            "job_dir": "",
            "mode": mode,
            "input_role": input_role,
        },
        task_metadata={
            "input_role": input_role,
            "mode": mode,
        },
        stage_metadata={"input_role": input_role, "mode": mode},
        manifest_overrides=manifest_overrides,
    )
    return _planned_engine_stage_payload(
        _EngineStageBuildRequest(
            workflow_id=workflow_id,
            stage_id=stage_id,
            spec=_CREST_STAGE_SPEC,
            priority=priority,
            max_cores=max_cores,
            max_memory_gb=max_memory_gb,
            sections=sections,
            input_artifacts=(
                WorkflowArtifactRef(
                    kind="input_xyz",
                    path=source_path,
                    selected=True,
                    metadata={"input_role": input_role},
                ),
            ),
        )
    )


__all__ = [
    "new_crest_stage_impl",
]
