from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from orca_auto.core.config.files import ORCA_AUTO_CONFIG_ENV_VAR, config_env_value
from orca_auto.core.config.schema import TelegramConfig
from orca_auto.core.messaging import (
    Message,
    MessageChannel,
    Severity,
    Span,
    TelegramChannel,
    build_channel_from_config_path,
    code,
    field_row,
    group,
    raw,
    title_heading,
)
from orca_auto.core.utils import coerce_mapping, normalize_text

from ..workflow._phases import SUPPRESSED_STAGE_NOTIFICATION_ENGINES, WORKFLOW_PHASE_FINISHED_EVENT
from ..workflow.store import resolve_workflow_workspace

DEFAULT_NOTIFICATION_EVENT_TYPES = frozenset(
    {
        "workflow_status_changed",
        "workflow_advance_failed",
        "worker_started",
        "worker_stopped",
        "worker_interrupted",
        "worker_lock_error",
    }
)
STAGE_STATUS_EVENT_TYPES = frozenset(
    {
        "workflow_stage_submitted",
        "workflow_stage_completed",
        "workflow_stage_failed",
        "workflow_stage_cancelled",
        "workflow_stage_status_changed",
    }
)
STAGE_HANDOFF_EVENT_TYPES = frozenset(
    {
        "workflow_stage_handoff_ready",
        "workflow_stage_handoff_retrying",
        "workflow_stage_handoff_failed",
        "workflow_stage_reaction_handoff_status_changed",
    }
)
_ERROR_EVENT_TYPES = frozenset(
    {
        "workflow_advance_failed",
        "workflow_stage_failed",
        "workflow_stage_handoff_failed",
        "worker_interrupted",
        "worker_lock_error",
    }
)
_SUCCESS_EVENT_TYPES = frozenset({"workflow_stage_completed", "workflow_stage_handoff_ready"})


def event_text(event: dict[str, Any], metadata: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text_value = normalize_text(event.get(key))
        if text_value:
            return text_value
        text_value = normalize_text(metadata.get(key))
        if text_value:
            return text_value
    return ""


def format_count_mapping(value: Any) -> str:
    mapping = coerce_mapping(value)
    if not mapping:
        return "-"
    parts: list[str] = []
    for key in sorted(mapping):
        try:
            count = int(mapping[key])
        except (TypeError, ValueError):
            continue
        parts.append(f"{normalize_text(key)}:{count}")
    return ",".join(parts) if parts else "-"


def format_stage_statuses(value: Any) -> str:
    if not isinstance(value, list):
        return "-"
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = normalize_text(item.get("label") or item.get("stage_id"))
        status = normalize_text(item.get("status") or item.get("task_status"))
        if not label or not status:
            continue
        parts.append(f"{label}:{status}")
    return ",".join(parts) if parts else "-"


def _transition_spans(previous: str, current: str) -> tuple[Span, ...]:
    previous_text = normalize_text(previous)
    current_text = normalize_text(current)
    if previous_text and current_text:
        return (code(previous_text), raw(" → "), code(current_text))
    if current_text:
        return (code(current_text),)
    if previous_text:
        return (code(previous_text),)
    return (code("-"),)


def title_from_event_type(event_type: str) -> str:
    labels = {
        "workflow_status_changed": "Status changed",
        "workflow_advance_failed": "Advance failed",
        "workflow_stage_submitted": "Stage submitted",
        "workflow_stage_completed": "Stage completed",
        "workflow_stage_failed": "Stage failed",
        "workflow_stage_cancelled": "Stage cancelled",
        "workflow_stage_status_changed": "Stage status changed",
        "workflow_stage_handoff_ready": "Handoff ready",
        "workflow_stage_handoff_retrying": "Handoff retrying",
        "workflow_stage_handoff_failed": "Handoff failed",
        "workflow_stage_reaction_handoff_status_changed": "Handoff status changed",
        WORKFLOW_PHASE_FINISHED_EVENT: "Phase finished",
        "worker_started": "Worker started",
        "worker_stopped": "Worker stopped",
        "worker_interrupted": "Worker interrupted",
        "worker_lock_error": "Worker lock error",
    }
    return labels.get(event_type, "Workflow event")


def _severity_for(event_type: str) -> Severity:
    if event_type in _ERROR_EVENT_TYPES:
        return "error"
    if event_type in _SUCCESS_EVENT_TYPES:
        return "success"
    if event_type == "workflow_stage_cancelled":
        return "warning"
    return "info"


def notification_event_types_from_env() -> set[str]:
    raw_value = os.environ.get("ORCA_AUTO_FLOW_NOTIFY_EVENT_TYPES", "")
    if not raw_value.strip():
        return set(DEFAULT_NOTIFICATION_EVENT_TYPES)
    return {item.strip() for item in raw_value.split(",") if item.strip()}


def journal_notification_enabled(event_type: str) -> bool:
    disabled = os.environ.get("ORCA_AUTO_FLOW_NOTIFY_DISABLED", "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return False
    return event_type in notification_event_types_from_env()


def messenger_channel_from_env() -> MessageChannel | None:
    """Resolve the active channel for journal notifications.

    When ``ORCA_AUTO_CONFIG`` is set, its messenger provider is authoritative.
    The legacy ``ORCA_AUTO_FLOW_TELEGRAM_*`` pair is used only when no shared
    config path is configured, so it cannot silently replace a Discord route.
    """
    config_path = config_env_value(ORCA_AUTO_CONFIG_ENV_VAR)
    if config_path:
        channel = build_channel_from_config_path(config_path)
        return channel if channel.enabled else None

    token = os.environ.get("ORCA_AUTO_FLOW_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ORCA_AUTO_FLOW_TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        return TelegramChannel(TelegramConfig(bot_token=token, chat_id=chat_id))

    return None


def _workspace_directory_text(workflow_id: str, workflow_root: str | Path) -> str:
    """Resolve the workspace directory a notification refers to, or ``-``."""

    if not workflow_id or workflow_id == "-":
        return "-"
    try:
        return str(resolve_workflow_workspace(target=workflow_id, workflow_root=workflow_root))
    except (FileNotFoundError, ValueError, OSError, RuntimeError):
        return "-"


def journal_event_context(event: dict[str, Any], workflow_root: str | Path) -> dict[str, str]:
    event_type = normalize_text(event.get("event_type"))
    metadata = coerce_mapping(event.get("metadata"))
    workflow_id = normalize_text(event.get("workflow_id")) or "-"
    return {
        "event_type": event_type,
        "workflow_id": workflow_id,
        "directory": _workspace_directory_text(workflow_id, workflow_root),
        "template_name": normalize_text(event.get("template_name")) or "-",
        "status": normalize_text(event.get("status")) or "-",
        "previous_status": normalize_text(event.get("previous_status")) or "-",
        "reason": normalize_text(event.get("reason")) or "-",
        "session": normalize_text(event.get("worker_session_id")) or "-",
        "stage_id": event_text(event, metadata, "stage_id") or "-",
        "engine": event_text(event, metadata, "engine") or "-",
        "task_kind": event_text(event, metadata, "task_kind") or "-",
        "stage_status": event_text(event, metadata, "stage_status", "status"),
        "previous_stage_status": event_text(
            event, metadata, "previous_stage_status", "previous_status"
        ),
        "reaction_handoff_status": event_text(event, metadata, "reaction_handoff_status"),
        "previous_reaction_handoff_status": event_text(
            event, metadata, "previous_reaction_handoff_status"
        ),
        "root_text": str(Path(workflow_root).expanduser().resolve()),
    }


def _message(context: dict[str, str], *fields: Any) -> Message:
    title = title_from_event_type(context["event_type"])
    return Message(
        title=title,
        severity=_severity_for(context["event_type"]),
        groups=(group(*fields, heading=title_heading(title)),),
        author="orca_auto",
    )


def workflow_status_event_message(context: dict[str, str]) -> Message:
    return _message(
        context,
        field_row("Workflow", code(context["workflow_id"]), inline=True),
        field_row("Template", code(context["template_name"]), inline=True),
        field_row("Status", *_transition_spans(context["previous_status"], context["status"])),
        field_row("Directory", code(context["directory"])),
    )


def workflow_advance_failed_event_message(context: dict[str, str]) -> Message:
    return _message(
        context,
        field_row("Workflow", code(context["workflow_id"]), inline=True),
        field_row("Template", code(context["template_name"]), inline=True),
        field_row("Reason", code(context["reason"])),
        field_row("Directory", code(context["directory"])),
    )


def stage_status_event_message(context: dict[str, str]) -> Message:
    task = f"{context['engine']}/{context['task_kind']}"
    fields = [
        field_row("Workflow", code(context["workflow_id"]), inline=True),
        field_row("Stage", code(context["stage_id"]), inline=True),
        field_row("Task", code(task), inline=True),
        field_row("Template", code(context["template_name"])),
        field_row(
            "Stage status",
            *_transition_spans(context["previous_stage_status"], context["stage_status"]),
        ),
        field_row("Directory", code(context["directory"])),
    ]
    if context["reason"] and context["reason"] != "-":
        fields.append(field_row("Reason", code(context["reason"])))
    return _message(context, *fields)


def stage_handoff_event_message(context: dict[str, str]) -> Message:
    task = f"{context['engine']}/{context['task_kind']}"
    fields = [
        field_row("Workflow", code(context["workflow_id"]), inline=True),
        field_row("Stage", code(context["stage_id"]), inline=True),
        field_row("Task", code(task), inline=True),
        field_row("Template", code(context["template_name"])),
        field_row(
            "Stage status",
            *_transition_spans(context["previous_stage_status"], context["stage_status"]),
        ),
        field_row(
            "Reaction handoff",
            *_transition_spans(
                context["previous_reaction_handoff_status"], context["reaction_handoff_status"]
            ),
        ),
        field_row("Directory", code(context["directory"])),
    ]
    if context["reason"] and context["reason"] != "-":
        fields.append(field_row("Reason", code(context["reason"])))
    return _message(context, *fields)


def worker_lifecycle_event_message(context: dict[str, str]) -> Message:
    return _message(
        context,
        field_row("Workflow root", code(context["root_text"])),
        field_row("Worker session", code(context["session"])),
        field_row("Reason", code(context["reason"])),
    )


def default_journal_event_message(context: dict[str, str]) -> Message:
    return _message(
        context,
        field_row("Event", code(context["event_type"])),
        field_row("Workflow", code(context["workflow_id"])),
        field_row("Status", code(context["status"])),
        field_row("Directory", code(context["directory"])),
    )


def _phase_finished_event_message(
    event: dict[str, Any], metadata: dict[str, Any], context: dict[str, str]
) -> Message:
    fields = [
        field_row("Workflow", code(context["workflow_id"])),
        field_row("Template", code(context["template_name"])),
        field_row(
            "Phase", code(event_text(event, metadata, "phase_label", "phase") or "-"), inline=True
        ),
        field_row(
            "Phase outcome",
            code(event_text(event, metadata, "phase_outcome", "status") or "-"),
            inline=True,
        ),
        field_row(
            "Stage count", code(event_text(event, metadata, "stage_count") or "0"), inline=True
        ),
        field_row(
            "Stage status counts", code(format_count_mapping(metadata.get("stage_status_counts")))
        ),
        field_row("Stage statuses", code(format_stage_statuses(metadata.get("stage_statuses")))),
        field_row("Directory", code(context["directory"])),
    ]
    handoff_counts = format_count_mapping(metadata.get("reaction_handoff_status_counts"))
    if handoff_counts != "-":
        fields.append(field_row("Reaction handoff counts", code(handoff_counts)))
    failure_reasons = metadata.get("failure_reasons")
    if isinstance(failure_reasons, list):
        joined = ",".join(normalize_text(item) for item in failure_reasons if normalize_text(item))
        if joined:
            fields.append(field_row("Failure reasons", code(joined)))
    return _message(context, *fields)


def journal_event_message(event: dict[str, Any], workflow_root: str | Path) -> Message:
    context = journal_event_context(event, workflow_root)
    event_type = context["event_type"]
    metadata = coerce_mapping(event.get("metadata"))

    if event_type == "workflow_status_changed":
        return workflow_status_event_message(context)
    if event_type == "workflow_advance_failed":
        return workflow_advance_failed_event_message(context)
    if event_type in STAGE_STATUS_EVENT_TYPES:
        return stage_status_event_message(context)
    if event_type in STAGE_HANDOFF_EVENT_TYPES:
        return stage_handoff_event_message(context)
    if event_type == WORKFLOW_PHASE_FINISHED_EVENT:
        return _phase_finished_event_message(event, metadata, context)
    if event_type in {
        "worker_started",
        "worker_stopped",
        "worker_interrupted",
        "worker_lock_error",
    }:
        return worker_lifecycle_event_message(context)
    return default_journal_event_message(context)


def should_suppress_stage_notification(event: dict[str, Any]) -> bool:
    event_type = normalize_text(event.get("event_type"))
    engine = event_text(event, coerce_mapping(event.get("metadata")), "engine")
    return (
        event_type in STAGE_STATUS_EVENT_TYPES | STAGE_HANDOFF_EVENT_TYPES
        and engine.lower() in SUPPRESSED_STAGE_NOTIFICATION_ENGINES
    )


__all__ = [
    "DEFAULT_NOTIFICATION_EVENT_TYPES",
    "STAGE_HANDOFF_EVENT_TYPES",
    "STAGE_STATUS_EVENT_TYPES",
    "journal_event_message",
    "journal_notification_enabled",
    "messenger_channel_from_env",
    "should_suppress_stage_notification",
]
