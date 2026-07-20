from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, TypedDict, cast

from orca_auto.core.utils.coercion import (
    coerce_mapping,
    normalize_text,
    safe_int,
)


class WorkflowArtifactRefPayload(TypedDict, total=False):
    kind: str
    path: str
    selected: bool
    metadata: dict[str, Any]


class WorkflowTaskPayload(TypedDict, total=False):
    task_id: str
    engine: str
    task_kind: str
    resource_request: dict[str, int]
    status: str
    payload: dict[str, Any]
    enqueue_payload: dict[str, Any]
    submission_result: dict[str, Any]
    depends_on: tuple[str, ...]
    metadata: dict[str, Any]


class WorkflowStagePayload(TypedDict, total=False):
    stage_id: str
    stage_kind: str
    status: str
    input_artifacts: list[WorkflowArtifactRefPayload]
    output_artifacts: list[WorkflowArtifactRefPayload]
    task: WorkflowTaskPayload | None
    metadata: dict[str, Any]


class WorkflowStageWithTaskPayload(TypedDict, total=False):
    stage_id: str
    stage_kind: str
    status: str
    input_artifacts: list[WorkflowArtifactRefPayload]
    output_artifacts: list[WorkflowArtifactRefPayload]
    task: WorkflowTaskPayload
    metadata: dict[str, Any]


class WorkflowTemplateRequestPayload(TypedDict, total=False):
    workflow_id: str
    template_name: str
    source_job_id: str
    source_job_type: str
    reaction_key: str
    status: str
    requested_at: str
    parameters: dict[str, Any]
    source_artifacts: list[WorkflowArtifactRefPayload]


class WorkflowPlanPayload(TypedDict, total=False):
    workflow_id: str
    template_name: str
    status: str
    source_job_id: str
    source_job_type: str
    reaction_key: str
    requested_at: str
    stages: list[WorkflowStagePayload]
    metadata: dict[str, Any]


def coerce_workflow_plan_payload(value: Any) -> WorkflowPlanPayload:
    return cast(WorkflowPlanPayload, coerce_mapping(value))


def workflow_stage_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    stages = payload.get("stages")
    if not isinstance(stages, list):
        return []
    return [stage for stage in stages if isinstance(stage, dict)]


def workflow_stage_metadata(stage: dict[str, Any]) -> dict[str, Any]:
    metadata = stage.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        stage["metadata"] = metadata
    return metadata


def workflow_task_payload_dict(task: dict[str, Any]) -> dict[str, Any]:
    payload = task.get("payload")
    if not isinstance(payload, dict):
        payload = {}
        task["payload"] = payload
    return payload


def workflow_request_parameters(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the durable workflow request parameters when structurally valid."""

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    request = metadata.get("request")
    if not isinstance(request, dict):
        return {}
    parameters = request.get("parameters")
    return parameters if isinstance(parameters, dict) else {}


@dataclass(frozen=True)
class WorkflowArtifactRef:
    kind: str
    path: str
    selected: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> WorkflowArtifactRefPayload:
        payload = asdict(self)
        if not self.metadata:
            payload["metadata"] = {}
        return cast(WorkflowArtifactRefPayload, payload)


@dataclass(frozen=True)
class WorkflowTask:
    task_id: str
    engine: str
    task_kind: str
    resource_request: dict[str, int]
    status: str = "planned"
    payload: dict[str, Any] = field(default_factory=dict)
    enqueue_payload: dict[str, Any] = field(default_factory=dict)
    submission_result: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(
        cls,
        *,
        task_id: str,
        engine: str,
        task_kind: str,
        status: str = "planned",
        resource_request: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        enqueue_payload: dict[str, Any] | None = None,
        submission_result: dict[str, Any] | None = None,
        depends_on: list[str] | tuple[str, ...] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorkflowTask:
        request = resource_request or {}
        return cls(
            task_id=normalize_text(task_id, none="None"),
            engine=normalize_text(engine, none="None") or "unknown",
            task_kind=normalize_text(task_kind, none="None") or "task",
            status=normalize_text(status, none="None") or "planned",
            resource_request={
                str(key): safe_int(value, default=0)
                for key, value in request.items()
                if normalize_text(key, none="None")
            },
            payload=coerce_mapping(payload),
            enqueue_payload=coerce_mapping(enqueue_payload),
            submission_result=coerce_mapping(submission_result),
            depends_on=tuple(
                text for item in (depends_on or ()) if (text := normalize_text(item, none="None"))
            ),
            metadata=coerce_mapping(metadata),
        )

    def to_dict(self) -> WorkflowTaskPayload:
        payload = asdict(self)
        if not self.payload:
            payload["payload"] = {}
        if not self.enqueue_payload:
            payload["enqueue_payload"] = {}
        if not self.submission_result:
            payload["submission_result"] = {}
        if not self.metadata:
            payload["metadata"] = {}
        return cast(WorkflowTaskPayload, payload)


@dataclass(frozen=True)
class WorkflowStage:
    stage_id: str
    stage_kind: str
    status: str
    input_artifacts: tuple[WorkflowArtifactRef, ...] = ()
    output_artifacts: tuple[WorkflowArtifactRef, ...] = ()
    task: WorkflowTask | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> WorkflowStagePayload:
        return {
            "stage_id": self.stage_id,
            "stage_kind": self.stage_kind,
            "status": self.status,
            "input_artifacts": [item.to_dict() for item in self.input_artifacts],
            "output_artifacts": [item.to_dict() for item in self.output_artifacts],
            "task": self.task.to_dict() if self.task is not None else None,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class WorkflowTemplateRequest:
    workflow_id: str
    template_name: str
    source_job_id: str
    source_job_type: str
    reaction_key: str
    status: str
    requested_at: str
    parameters: dict[str, Any] = field(default_factory=dict)
    source_artifacts: tuple[WorkflowArtifactRef, ...] = ()

    def to_dict(self) -> WorkflowTemplateRequestPayload:
        return {
            "workflow_id": self.workflow_id,
            "template_name": self.template_name,
            "source_job_id": self.source_job_id,
            "source_job_type": self.source_job_type,
            "reaction_key": self.reaction_key,
            "status": self.status,
            "requested_at": self.requested_at,
            "parameters": dict(self.parameters),
            "source_artifacts": [item.to_dict() for item in self.source_artifacts],
        }


@dataclass(frozen=True)
class WorkflowPlan:
    workflow_id: str
    template_name: str
    status: str
    source_job_id: str
    source_job_type: str
    reaction_key: str
    requested_at: str
    stages: tuple[WorkflowStage, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> WorkflowPlanPayload:
        return {
            "workflow_id": self.workflow_id,
            "template_name": self.template_name,
            "status": self.status,
            "source_job_id": self.source_job_id,
            "source_job_type": self.source_job_type,
            "reaction_key": self.reaction_key,
            "requested_at": self.requested_at,
            "stages": [item.to_dict() for item in self.stages],
            "metadata": dict(self.metadata),
        }


__all__ = [
    "WorkflowArtifactRef",
    "WorkflowArtifactRefPayload",
    "WorkflowPlan",
    "WorkflowPlanPayload",
    "WorkflowStage",
    "WorkflowStagePayload",
    "WorkflowStageWithTaskPayload",
    "WorkflowTask",
    "WorkflowTaskPayload",
    "WorkflowTemplateRequest",
    "WorkflowTemplateRequestPayload",
    "coerce_workflow_plan_payload",
    "workflow_request_parameters",
]
