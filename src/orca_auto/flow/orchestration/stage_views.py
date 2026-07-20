from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orca_auto.core.utils import normalize_text
from orca_auto.flow.contracts.workflow import workflow_stage_metadata, workflow_task_payload_dict
from orca_auto.flow.orchestration.stage_view_mutators import (
    WorkflowStageCrestMutationMixin,
    WorkflowStageOrcaMutationMixin,
    WorkflowStageXtbMutationMixin,
    WorkflowTaskCrestMutationMixin,
    WorkflowTaskOrcaMutationMixin,
    WorkflowTaskXtbMutationMixin,
)


def _mapping_field(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if isinstance(value, dict):
        return value
    value = {}
    raw[key] = value
    return value


def _existing_mapping_field(raw: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = raw.get(key)
    return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class WorkflowTaskView(
    WorkflowTaskOrcaMutationMixin,
    WorkflowTaskCrestMutationMixin,
    WorkflowTaskXtbMutationMixin,
):
    raw: dict[str, Any]

    def existing_mapping(self, key: str) -> dict[str, Any] | None:
        return _existing_mapping_field(self.raw, key)

    def payload(self) -> dict[str, Any]:
        return workflow_task_payload_dict(self.raw)

    def metadata(self) -> dict[str, Any]:
        return _mapping_field(self.raw, "metadata")

    def enqueue_payload(self) -> dict[str, Any]:
        return _mapping_field(self.raw, "enqueue_payload")

    def existing_enqueue_payload(self) -> dict[str, Any] | None:
        return self.existing_mapping("enqueue_payload")

    def existing_payload(self) -> dict[str, Any] | None:
        return self.existing_mapping("payload")

    def submission_result(self) -> dict[str, Any]:
        return _mapping_field(self.raw, "submission_result")

    def existing_submission_result(self) -> dict[str, Any] | None:
        return self.existing_mapping("submission_result")

    def resource_request(self) -> dict[str, Any]:
        return _mapping_field(self.raw, "resource_request")

    def text_field(self, key: str, normalize_text: Callable[[Any], str]) -> str:
        return normalize_text(self.raw.get(key))

    def engine(self) -> str:
        return normalize_text(self.raw.get("engine")).lower()

    def kind(self) -> str:
        return normalize_text(self.raw.get("task_kind")).lower()

    def status(self) -> str:
        return normalize_text(self.raw.get("status")).lower()

    def status_with(self, normalize_text: Callable[[Any], str]) -> str:
        return self.text_field("status", normalize_text).lower()

    def has_submitted_result(self) -> bool:
        submission = self.existing_submission_result()
        return submission is not None and submission.get("status") == "submitted"

    def set_status(self, status: str) -> None:
        self.raw["status"] = status

    def set_submission_result(self, submission: dict[str, Any]) -> None:
        self.raw["submission_result"] = submission

    def set_cancel_result(self, result: dict[str, Any]) -> None:
        self.raw["cancel_result"] = result

    def clear_keys(self, *keys: str) -> None:
        for key in keys:
            self.raw.pop(key, None)

    def update_resource_request(self, resources: dict[str, int]) -> None:
        if resources:
            self.raw["resource_request"] = {**self.resource_request(), **resources}

    def set_payload_field(self, key: str, value: Any) -> None:
        self.payload()[key] = value

    def update_payload(self, fields: dict[str, Any]) -> None:
        self.payload().update(fields)

    def clear_payload_keys(self, *keys: str) -> None:
        payload = self.payload()
        for key in keys:
            payload.pop(key, None)

    def set_existing_payload_fields(self, keys: set[str] | frozenset[str], value: Any) -> None:
        payload = self.payload()
        for key in keys:
            if key in payload:
                payload[key] = value

    def update_enqueue_payload(self, fields: dict[str, Any]) -> None:
        self.enqueue_payload().update(fields)

    def set_existing_enqueue_payload_field(self, key: str, value: Any) -> None:
        payload = self.enqueue_payload()
        if key in payload:
            payload[key] = value

    def set_metadata_field(self, key: str, value: Any) -> None:
        self.metadata()[key] = value

    def update_metadata(self, fields: dict[str, Any]) -> None:
        self.metadata().update(fields)

    def clear_metadata_keys(self, *keys: str) -> None:
        metadata = self.metadata()
        for key in keys:
            metadata.pop(key, None)


@dataclass(frozen=True)
class WorkflowStageStatus:
    stage: str
    task: str

    def any_status(self, *statuses: str) -> bool:
        targets = set(statuses)
        return self.stage in targets or self.task in targets

    def any_matches(self, predicate: Callable[[str], bool]) -> bool:
        return predicate(self.stage) or predicate(self.task)


@dataclass(frozen=True)
class WorkflowStageView(
    WorkflowStageOrcaMutationMixin,
    WorkflowStageCrestMutationMixin,
    WorkflowStageXtbMutationMixin,
):
    raw: dict[str, Any]

    @classmethod
    def from_raw(cls, value: Any) -> WorkflowStageView | None:
        return cls(value) if isinstance(value, dict) else None

    @property
    def task(self) -> WorkflowTaskView:
        task = self.raw.get("task")
        return WorkflowTaskView(task if isinstance(task, dict) else {})

    @property
    def existing_task(self) -> WorkflowTaskView | None:
        task = self.raw.get("task")
        return WorkflowTaskView(task) if isinstance(task, dict) else None

    @property
    def has_task(self) -> bool:
        return isinstance(self.raw.get("task"), dict)

    def ensure_task(self) -> WorkflowTaskView:
        task = self.raw.get("task")
        if not isinstance(task, dict):
            task = {}
            self.raw["task"] = task
        return WorkflowTaskView(task)

    def metadata(self) -> dict[str, Any]:
        return workflow_stage_metadata(self.raw)

    def existing_metadata(self) -> dict[str, Any] | None:
        return _existing_mapping_field(self.raw, "metadata")

    def text_field(self, key: str, normalize_text: Callable[[Any], str]) -> str:
        return normalize_text(self.raw.get(key))

    def stage_id(self) -> str:
        return normalize_text(self.raw.get("stage_id"))

    def stage_id_with(self, normalize_text: Callable[[Any], str]) -> str:
        return self.text_field("stage_id", normalize_text)

    def status(self) -> str:
        return normalize_text(self.raw.get("status")).lower()

    def status_with(self, normalize_text: Callable[[Any], str]) -> str:
        return self.text_field("status", normalize_text).lower()

    def status_pair(self) -> WorkflowStageStatus:
        return WorkflowStageStatus(stage=self.status(), task=self.task_status())

    def status_pair_with(self, normalize_text: Callable[[Any], str]) -> WorkflowStageStatus:
        task = self.existing_task
        return WorkflowStageStatus(
            stage=self.status_with(normalize_text),
            task=task.status_with(normalize_text) if task is not None else "",
        )

    def set_status(self, status: str) -> None:
        self.raw["status"] = status

    def set_status_pair(self, *, stage_status: str, task_status: str) -> None:
        self.set_status(stage_status)
        if self.has_task:
            self.task.set_status(task_status)

    def set_output_artifacts(self, artifacts: list[dict[str, Any]]) -> None:
        self.raw["output_artifacts"] = artifacts

    def set_metadata_field(self, key: str, value: Any) -> None:
        self.metadata()[key] = value

    def update_metadata(self, fields: dict[str, Any]) -> None:
        self.metadata().update(fields)

    def clear_metadata_keys(self, *keys: str) -> None:
        metadata = self.metadata()
        for key in keys:
            metadata.pop(key, None)

    def task_engine(self) -> str:
        return self.task.engine()

    def task_kind(self) -> str:
        return self.task.kind()

    def task_status(self) -> str:
        return self.task.status()


@dataclass(frozen=True)
class WorkflowPayloadView:
    raw: dict[str, Any]

    @property
    def stage_views(self) -> list[WorkflowStageView]:
        return _stage_views(self.raw)

    def metadata(self) -> dict[str, Any] | None:
        metadata = self.raw.setdefault("metadata", {})
        return metadata if isinstance(metadata, dict) else None

    def workflow_id(self, normalize_text: Callable[[Any], str] | None = None) -> str:
        value = self.raw.get("workflow_id", "")
        return normalize_text(value) if normalize_text is not None else str(value)

    def status(self, normalize_text: Callable[[Any], str]) -> str:
        return normalize_text(self.raw.get("status")).lower()

    def set_status(self, status: str) -> None:
        self.raw["status"] = status


def _stage_views(payload: dict[str, Any]) -> list[WorkflowStageView]:
    return [
        view
        for raw_stage in payload.get("stages", [])
        if (view := WorkflowStageView.from_raw(raw_stage)) is not None
    ]


def _engine_stages(payload: dict[str, Any], engine: str) -> list[dict[str, Any]]:
    return [view.raw for view in _stage_views(payload) if view.task_engine() == engine]


def _engine_stage_views(payload: dict[str, Any], engine: str) -> list[WorkflowStageView]:
    return [view for view in _stage_views(payload) if view.task_engine() == engine]


def _clear_workflow_error_scope(payload_metadata: dict[str, Any], scopes: set[str]) -> None:
    workflow_error = payload_metadata.get("workflow_error")
    if isinstance(workflow_error, dict) and normalize_text(workflow_error.get("scope")) in scopes:
        payload_metadata.pop("workflow_error", None)
