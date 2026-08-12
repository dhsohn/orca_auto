from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orca_auto.core.utils.coercion import normalize_text

from .stage_event_metadata import (
    stage_event_metadata,
    stage_handoff_event_type,
    stage_key,
    stage_status_event_type,
    stage_transition_context,
    stage_transition_metadata,
)

if TYPE_CHECKING:
    from .runtime.models import StageTransitionContext, WorkflowJournalEventPayload


@dataclass(frozen=True)
class _WorkflowEventContext:
    workflow_id: str
    template_name: str
    worker_session_id: str

    def base_payload(self, event_type: str) -> WorkflowJournalEventPayload:
        return {
            "event_type": event_type,
            "workflow_id": self.workflow_id,
            "template_name": self.template_name,
            "worker_session_id": self.worker_session_id,
        }


@dataclass(frozen=True)
class _StageTransitionEventRequest:
    event_type: str
    current_stage: dict[str, Any]
    context: StageTransitionContext
    metadata: dict[str, Any]
    workflow: _WorkflowEventContext

    def base_payload(self) -> WorkflowJournalEventPayload:
        payload = self.workflow.base_payload(self.event_type)
        payload.update(
            {
                "stage_id": self.context["stage_id"],
                "engine": self.context["engine"],
                "task_kind": self.context["task_kind"],
                "stage_status": self.context["current_stage_status"],
                "previous_stage_status": self.context["previous_stage_status"],
            }
        )
        return payload

    def status_payload(self) -> WorkflowJournalEventPayload:
        reason = ""
        if self.event_type in {"workflow_stage_failed", "workflow_stage_cancelled"}:
            reason = normalize_text(self.current_stage.get("reason"))
        payload = self.base_payload()
        payload.update(
            {
                "status": self.context["current_stage_status"],
                "previous_status": self.context["previous_stage_status"],
                "reason": reason,
                "metadata": stage_transition_metadata(
                    self.metadata,
                    self.context,
                    include_handoff=False,
                ),
            }
        )
        return payload

    def handoff_payload(self) -> WorkflowJournalEventPayload:
        payload = self.base_payload()
        payload.update(
            {
                "status": self.context["current_handoff_status"],
                "previous_status": self.context["previous_handoff_status"],
                "reason": normalize_text(
                    self.current_stage.get("reaction_handoff_reason")
                    or self.current_stage.get("reason")
                ),
                "reaction_handoff_status": self.context["current_handoff_status"],
                "previous_reaction_handoff_status": self.context["previous_handoff_status"],
                "metadata": stage_transition_metadata(
                    self.metadata,
                    self.context,
                    include_handoff=True,
                ),
            }
        )
        return payload


def _stage_transition_event_request(
    *,
    event_type: str,
    current_stage: dict[str, Any],
    context: StageTransitionContext,
    metadata: dict[str, Any],
    workflow_id: str,
    template_name: str,
    worker_session_id: str,
) -> _StageTransitionEventRequest:
    return _StageTransitionEventRequest(
        event_type=event_type,
        current_stage=current_stage,
        context=context,
        metadata=metadata,
        workflow=_WorkflowEventContext(
            workflow_id=workflow_id,
            template_name=template_name,
            worker_session_id=worker_session_id,
        ),
    )


def status_transition_event_payload(
    *,
    event_type: str,
    current_stage: dict[str, Any],
    context: StageTransitionContext,
    metadata: dict[str, Any],
    workflow_id: str,
    template_name: str,
    worker_session_id: str,
) -> WorkflowJournalEventPayload:
    return _stage_transition_event_request(
        event_type=event_type,
        current_stage=current_stage,
        context=context,
        metadata=metadata,
        workflow_id=workflow_id,
        template_name=template_name,
        worker_session_id=worker_session_id,
    ).status_payload()


def handoff_transition_event_payload(
    *,
    event_type: str,
    current_stage: dict[str, Any],
    context: StageTransitionContext,
    metadata: dict[str, Any],
    workflow_id: str,
    template_name: str,
    worker_session_id: str,
) -> WorkflowJournalEventPayload:
    return _stage_transition_event_request(
        event_type=event_type,
        current_stage=current_stage,
        context=context,
        metadata=metadata,
        workflow_id=workflow_id,
        template_name=template_name,
        worker_session_id=worker_session_id,
    ).handoff_payload()


def stage_transition_event_payloads(
    *,
    previous_summary: dict[str, Any],
    current_summary: dict[str, Any],
    workflow_id: str,
    template_name: str,
    worker_session_id: str,
) -> list[WorkflowJournalEventPayload]:
    previous_stages = list(previous_summary.get("stage_summaries", []))
    current_stages = list(current_summary.get("stage_summaries", []))
    previous_by_key = {
        stage_key(stage, index): dict(stage) for index, stage in enumerate(previous_stages)
    }
    event_payloads: list[WorkflowJournalEventPayload] = []

    for index, raw_stage in enumerate(current_stages):
        current_stage = dict(raw_stage)
        previous_stage = previous_by_key.get(stage_key(current_stage, index), {})
        handoff_event_type = stage_handoff_event_type(previous_stage, current_stage)
        status_event_type = stage_status_event_type(
            previous_stage,
            current_stage,
            suppress_terminal_event=handoff_event_type
            in {"workflow_stage_handoff_ready", "workflow_stage_handoff_failed"},
        )
        metadata = stage_event_metadata(current_stage)
        context = stage_transition_context(previous_stage, current_stage)

        if status_event_type:
            event_payloads.append(
                status_transition_event_payload(
                    event_type=status_event_type,
                    current_stage=current_stage,
                    context=context,
                    metadata=metadata,
                    workflow_id=workflow_id,
                    template_name=template_name,
                    worker_session_id=worker_session_id,
                )
            )

        if handoff_event_type:
            event_payloads.append(
                handoff_transition_event_payload(
                    event_type=handoff_event_type,
                    current_stage=current_stage,
                    context=context,
                    metadata=metadata,
                    workflow_id=workflow_id,
                    template_name=template_name,
                    worker_session_id=worker_session_id,
                )
            )
    return event_payloads


def append_stage_transition_events(
    workflow_root: str | Path,
    *,
    previous_summary: dict[str, Any],
    current_summary: dict[str, Any],
    workflow_id: str,
    template_name: str,
    worker_session_id: str,
    stage_transition_event_payloads_fn: Callable[..., list[WorkflowJournalEventPayload]],
    append_workflow_journal_event_fn: Callable[..., Any],
) -> None:
    for payload in stage_transition_event_payloads_fn(
        previous_summary=previous_summary,
        current_summary=current_summary,
        workflow_id=workflow_id,
        template_name=template_name,
        worker_session_id=worker_session_id,
    ):
        append_workflow_journal_event_fn(workflow_root, **payload)


def append_phase_transition_events(
    workflow_root: str | Path,
    *,
    previous_summary: dict[str, Any],
    current_summary: dict[str, Any],
    workflow_id: str,
    template_name: str,
    worker_session_id: str,
    phase_transition_event_payloads_fn: Callable[..., list[dict[str, Any]]],
    append_workflow_journal_event_fn: Callable[..., Any],
) -> None:
    for payload in phase_transition_event_payloads_fn(
        previous_summary=previous_summary,
        current_summary=current_summary,
        workflow_id=workflow_id,
        template_name=template_name,
        worker_session_id=worker_session_id,
    ):
        append_workflow_journal_event_fn(workflow_root, **payload)


def append_workflow_advance_failed_event(
    workflow_root: str | Path,
    record: Any,
    *,
    previous_status: str,
    reason: str,
    worker_session_id: str,
    append_workflow_journal_event_fn: Callable[..., Any],
) -> None:
    event_kwargs = _WorkflowEventContext(
        workflow_id=record.workflow_id,
        template_name=record.template_name,
        worker_session_id=worker_session_id,
    ).base_payload("workflow_advance_failed")
    event_kwargs.update(
        {
            "previous_status": previous_status,
            "status": "advance_failed",
            "reason": reason,
        }
    )
    append_workflow_journal_event_fn(workflow_root, **event_kwargs)


def append_workflow_advanced_events(
    workflow_root: str | Path,
    record: Any,
    payload: dict[str, Any],
    *,
    previous_status: str,
    current_summary: dict[str, Any],
    previous_summary: dict[str, Any],
    worker_session_id: str,
    reason: str = "",
    append_workflow_journal_event_fn: Callable[..., Any],
    append_phase_transition_events_fn: Callable[..., None],
    append_stage_transition_events_fn: Callable[..., None],
    normalize_text_fn: Callable[[Any], str] = normalize_text,
) -> None:
    status = normalize_text_fn(payload.get("status")).lower()
    workflow_id = normalize_text_fn(payload.get("workflow_id")) or record.workflow_id
    template_name = normalize_text_fn(payload.get("template_name")) or record.template_name
    if status != previous_status:
        event_kwargs = _WorkflowEventContext(
            workflow_id=workflow_id,
            template_name=template_name,
            worker_session_id=worker_session_id,
        ).base_payload("workflow_status_changed")
        event_kwargs.update({"previous_status": previous_status, "status": status})
        if reason:
            event_kwargs["reason"] = reason
        append_workflow_journal_event_fn(workflow_root, **event_kwargs)
    append_phase_transition_events_fn(
        workflow_root,
        previous_summary=previous_summary,
        current_summary=current_summary,
        workflow_id=workflow_id,
        template_name=template_name,
        worker_session_id=worker_session_id,
    )
    append_stage_transition_events_fn(
        workflow_root,
        previous_summary=previous_summary,
        current_summary=current_summary,
        workflow_id=workflow_id,
        template_name=template_name,
        worker_session_id=worker_session_id,
    )


__all__ = [
    "_StageTransitionEventRequest",
    "_WorkflowEventContext",
    "_stage_transition_event_request",
    "append_phase_transition_events",
    "append_stage_transition_events",
    "append_workflow_advance_failed_event",
    "append_workflow_advanced_events",
    "handoff_transition_event_payload",
    "stage_transition_event_payloads",
    "status_transition_event_payload",
]
