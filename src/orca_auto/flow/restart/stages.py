from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orca_auto.core.app_ids import is_orca_submitter
from orca_auto.core.utils import (
    mapping_or_empty as _coerce_mapping,
)
from orca_auto.core.utils import (
    normalize_text as _normalize_text,
)
from orca_auto.flow.orchestration.stage_views import (
    WorkflowPayloadView,
    WorkflowStageView,
    WorkflowTaskView,
)


@dataclass(frozen=True)
class RestartStageContext:
    active_stage_statuses: frozenset[str]
    rematerialized_engines: frozenset[str]
    rematerialized_task_payload_keys: frozenset[str]
    restartable_stage_statuses: frozenset[str]
    stale_stage_metadata_keys: frozenset[str]
    stale_task_payload_keys: frozenset[str]
    coerce_mapping: Callable[[Any], dict[str, Any]] = _coerce_mapping
    normalize_text: Callable[[Any], str] = _normalize_text

    def stage_status(self, stage_view: WorkflowStageView) -> str:
        return stage_view.status_with(self.normalize_text)

    def task_status(self, task_view: WorkflowTaskView | None) -> str:
        if task_view is None:
            return ""
        return task_view.status_with(self.normalize_text)

    def task_engine(self, task_view: WorkflowTaskView | None) -> str:
        if task_view is None:
            return ""
        return task_view.text_field("engine", self.normalize_text).lower()

    def task_is_orca(self, task_view: WorkflowTaskView) -> bool:
        engine = self.task_engine(task_view)
        if engine == "orca":
            return True
        enqueue_payload = self.coerce_mapping(task_view.raw.get("enqueue_payload"))
        return is_orca_submitter(enqueue_payload.get("submitter"))


def stage_needs_restart(stage: dict[str, Any], *, context: RestartStageContext) -> bool:
    stage_view = WorkflowStageView(stage)
    task_view = stage_view.existing_task
    stage_status = context.stage_status(stage_view)
    task_status = context.task_status(task_view)
    if stage_status == "completed" and task_status == "completed":
        return False
    return (
        stage_status in context.restartable_stage_statuses
        or task_status in context.restartable_stage_statuses
    )


def active_stage_rows(
    payload: dict[str, Any], *, context: RestartStageContext
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for stage_view in WorkflowPayloadView(payload).stage_views:
        task_view = stage_view.existing_task
        stage_status = context.stage_status(stage_view)
        task_status = context.task_status(task_view)
        if (
            stage_status not in context.active_stage_statuses
            and task_status not in context.active_stage_statuses
        ):
            continue
        rows.append(
            {
                "stage_id": stage_view.stage_id_with(context.normalize_text),
                "status": stage_status,
                "task_status": task_status,
                "engine": context.task_engine(task_view),
            }
        )
    return rows


def active_restart_error(workflow_id: str, rows: list[dict[str, str]]) -> ValueError:
    shown = []
    for row in rows[:5]:
        stage_id = row.get("stage_id") or "stage"
        status = row.get("status") or "-"
        task_status = row.get("task_status") or "-"
        shown.append(f"{stage_id}(status={status}, task_status={task_status})")
    suffix = f"; active_stages={', '.join(shown)}" if shown else ""
    if len(rows) > len(shown):
        suffix += f"; remaining_active_count={len(rows) - len(shown)}"
    return ValueError(
        f"workflow still has active stages; wait for cancellation/sync to finish before restart: "
        f"{workflow_id}{suffix}"
    )


def clear_phase_notification_state(
    metadata: dict[str, Any],
    restarted_stages: list[dict[str, str]],
    *,
    context: RestartStageContext,
) -> None:
    phase_notifications = metadata.get("phase_notifications")
    if not isinstance(phase_notifications, dict):
        return

    engines = {
        context.normalize_text(stage.get("engine")).lower()
        for stage in restarted_stages
        if context.normalize_text(stage.get("engine"))
    }
    for engine in engines:
        phase_notifications.pop(f"{engine}_summary", None)
    if not phase_notifications:
        metadata.pop("phase_notifications", None)


def reset_stage_for_restart(
    stage: dict[str, Any],
    *,
    rematerialize: bool = False,
    context: RestartStageContext,
) -> dict[str, str]:
    stage_view = WorkflowStageView(stage)
    task_view = stage_view.ensure_task()
    engine = context.task_engine(task_view)

    previous = {
        "stage_id": stage_view.stage_id_with(context.normalize_text),
        "previous_status": stage_view.text_field("status", context.normalize_text),
        "previous_task_status": task_view.text_field("status", context.normalize_text),
        "engine": task_view.text_field("engine", context.normalize_text),
    }

    stage_view.set_status_pair(stage_status="planned", task_status="planned")
    stage_view.set_output_artifacts([])
    task_view.clear_keys("submission_result", "cancel_result")

    stage_view.clear_metadata_keys(*context.stale_stage_metadata_keys)
    task_view.clear_payload_keys(*context.stale_task_payload_keys)

    if rematerialize and engine in context.rematerialized_engines:
        task_view.set_existing_payload_fields(context.rematerialized_task_payload_keys, "")
        task_view.set_existing_enqueue_payload_field("job_dir", "")

    if context.task_is_orca(task_view):
        task_view.update_enqueue_payload({"force": True})

    return previous
