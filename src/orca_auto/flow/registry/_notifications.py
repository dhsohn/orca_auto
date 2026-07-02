from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from orca_auto.core.config.files import ORCA_AUTO_CONFIG_ENV_VAR, config_env_value
from orca_auto.core.config.schema import TelegramConfig
from orca_auto.core.notifications import (
    build_telegram_transport,
    escape_html,
    html_code,
    load_telegram_config_from_file,
)
from orca_auto.core.utils import coerce_mapping, normalize_text

from ..workflow._phases import SUPPRESSED_STAGE_NOTIFICATION_ENGINES, WORKFLOW_PHASE_FINISHED_EVENT

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


def event_text(event: dict[str, Any], metadata: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = normalize_text(event.get(key))
        if text:
            return text
        text = normalize_text(metadata.get(key))
        if text:
            return text
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


def transition_html(previous: str, current: str) -> str:
    previous_text = normalize_text(previous)
    current_text = normalize_text(current)
    if previous_text and current_text:
        return f"{html_code(previous_text)} -> {html_code(current_text)}"
    if current_text:
        return html_code(current_text)
    if previous_text:
        return html_code(previous_text)
    return html_code("-")


def title_from_event_type(event_type: str) -> str:
    labels = {
        "workflow_status_changed": "Status Changed",
        "workflow_advance_failed": "Advance Failed",
        "workflow_stage_submitted": "Stage Submitted",
        "workflow_stage_completed": "Stage Completed",
        "workflow_stage_failed": "Stage Failed",
        "workflow_stage_cancelled": "Stage Cancelled",
        "workflow_stage_status_changed": "Stage Status Changed",
        "workflow_stage_handoff_ready": "Handoff Ready",
        "workflow_stage_handoff_retrying": "Handoff Retrying",
        "workflow_stage_handoff_failed": "Handoff Failed",
        "workflow_stage_reaction_handoff_status_changed": "Handoff Status Changed",
        WORKFLOW_PHASE_FINISHED_EVENT: "Phase Finished",
        "worker_started": "Worker Started",
        "worker_stopped": "Worker Stopped",
        "worker_interrupted": "Worker Interrupted",
        "worker_lock_error": "Worker Lock Error",
    }
    return f"orca_auto Flow {labels.get(event_type, 'Event')}"


def notification_event_types_from_env() -> set[str]:
    raw = os.environ.get("ORCA_AUTO_FLOW_NOTIFY_EVENT_TYPES", "")
    if not raw.strip():
        return set(DEFAULT_NOTIFICATION_EVENT_TYPES)
    return {item.strip() for item in raw.split(",") if item.strip()}


def journal_notification_enabled(event_type: str) -> bool:
    disabled = os.environ.get("ORCA_AUTO_FLOW_NOTIFY_DISABLED", "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return False
    return event_type in notification_event_types_from_env()


def telegram_transport_from_env():
    token = os.environ.get("ORCA_AUTO_FLOW_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ORCA_AUTO_FLOW_TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        return build_telegram_transport(TelegramConfig(bot_token=token, chat_id=chat_id))

    config_path = config_env_value(ORCA_AUTO_CONFIG_ENV_VAR)
    if not config_path:
        return None
    telegram = load_telegram_config_from_file(config_path)
    if not telegram.enabled:
        return None
    return build_telegram_transport(telegram)


def journal_event_context(event: dict[str, Any], workflow_root: str | Path) -> dict[str, str]:
    event_type = normalize_text(event.get("event_type"))
    metadata = coerce_mapping(event.get("metadata"))
    return {
        "event_type": event_type,
        "workflow_id": normalize_text(event.get("workflow_id")) or "-",
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


def workflow_status_event_message(context: dict[str, str]) -> str:
    event_type = context["event_type"]
    return "\n".join(
        [
            f"<b>{escape_html(title_from_event_type(event_type))}</b>",
            f"<b>Workflow</b>: {html_code(context['workflow_id'])}",
            f"<b>Template</b>: {html_code(context['template_name'])}",
            f"<b>Status</b>: {transition_html(context['previous_status'], context['status'])}",
            f"<b>Worker session</b>: {html_code(context['session'])}",
        ]
    )


def workflow_advance_failed_event_message(context: dict[str, str]) -> str:
    event_type = context["event_type"]
    return "\n".join(
        [
            f"<b>{escape_html(title_from_event_type(event_type))}</b>",
            f"<b>Workflow</b>: {html_code(context['workflow_id'])}",
            f"<b>Template</b>: {html_code(context['template_name'])}",
            f"<b>Reason</b>: {html_code(context['reason'])}",
            f"<b>Worker session</b>: {html_code(context['session'])}",
        ]
    )


def stage_status_event_message(context: dict[str, str]) -> str:
    event_type = context["event_type"]
    task = f"{context['engine']}/{context['task_kind']}"
    lines = [
        f"<b>{escape_html(title_from_event_type(event_type))}</b>",
        f"<b>Workflow</b>: {html_code(context['workflow_id'])}",
        f"<b>Template</b>: {html_code(context['template_name'])}",
        f"<b>Event</b>: {html_code(event_type)}",
        f"<b>Stage</b>: {html_code(context['stage_id'])}",
        f"<b>Task</b>: {html_code(task)}",
        f"<b>Stage status</b>: {transition_html(context['previous_stage_status'], context['stage_status'])}",
        f"<b>Worker session</b>: {html_code(context['session'])}",
    ]
    if context["reason"]:
        lines.append(f"<b>Reason</b>: {html_code(context['reason'])}")
    return "\n".join(lines)


def stage_handoff_event_message(context: dict[str, str]) -> str:
    event_type = context["event_type"]
    task = f"{context['engine']}/{context['task_kind']}"
    lines = [
        f"<b>{escape_html(title_from_event_type(event_type))}</b>",
        f"<b>Workflow</b>: {html_code(context['workflow_id'])}",
        f"<b>Template</b>: {html_code(context['template_name'])}",
        f"<b>Event</b>: {html_code(event_type)}",
        f"<b>Stage</b>: {html_code(context['stage_id'])}",
        f"<b>Task</b>: {html_code(task)}",
        f"<b>Stage status</b>: {transition_html(context['previous_stage_status'], context['stage_status'])}",
        (
            "<b>Reaction handoff</b>: "
            f"{transition_html(context['previous_reaction_handoff_status'], context['reaction_handoff_status'])}"
        ),
        f"<b>Worker session</b>: {html_code(context['session'])}",
    ]
    if context["reason"]:
        lines.append(f"<b>Reason</b>: {html_code(context['reason'])}")
    return "\n".join(lines)


def worker_lifecycle_event_message(context: dict[str, str]) -> str:
    event_type = context["event_type"]
    return "\n".join(
        [
            f"<b>{escape_html(title_from_event_type(event_type))}</b>",
            f"<b>Event</b>: {html_code(event_type)}",
            f"<b>Workflow root</b>: {html_code(context['root_text'])}",
            f"<b>Worker session</b>: {html_code(context['session'])}",
            f"<b>Reason</b>: {html_code(context['reason'])}",
        ]
    )


def default_journal_event_message(context: dict[str, str]) -> str:
    event_type = context["event_type"]
    return "\n".join(
        [
            f"<b>{escape_html(title_from_event_type(event_type))}</b>",
            f"<b>Event</b>: {html_code(event_type)}",
            f"<b>Workflow</b>: {html_code(context['workflow_id'])}",
            f"<b>Status</b>: {html_code(context['status'])}",
            f"<b>Worker session</b>: {html_code(context['session'])}",
        ]
    )


def journal_event_message(event: dict[str, Any], workflow_root: str | Path) -> str:
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
        lines = [
            f"<b>{escape_html(title_from_event_type(event_type))}</b>",
            f"<b>Workflow</b>: {html_code(context['workflow_id'])}",
            f"<b>Template</b>: {html_code(context['template_name'])}",
            f"<b>Event</b>: {html_code(event_type)}",
            f"<b>Phase</b>: {html_code(event_text(event, metadata, 'phase_label', 'phase') or '-')}",
            f"<b>Phase outcome</b>: {html_code(event_text(event, metadata, 'phase_outcome', 'status') or '-')}",
            f"<b>Stage count</b>: {html_code(event_text(event, metadata, 'stage_count') or '0')}",
            f"<b>Stage status counts</b>: {html_code(format_count_mapping(metadata.get('stage_status_counts')))}",
            f"<b>Stage statuses</b>: {html_code(format_stage_statuses(metadata.get('stage_statuses')))}",
            f"<b>Worker session</b>: {html_code(context['session'])}",
        ]
        handoff_counts = format_count_mapping(metadata.get("reaction_handoff_status_counts"))
        if handoff_counts != "-":
            lines.append(f"<b>Reaction handoff counts</b>: {html_code(handoff_counts)}")
        failure_reasons = metadata.get("failure_reasons")
        if isinstance(failure_reasons, list):
            joined = ",".join(
                normalize_text(item) for item in failure_reasons if normalize_text(item)
            )
            if joined:
                lines.append(f"<b>Failure reasons</b>: {html_code(joined)}")
        return "\n".join(lines)
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
