"""Command handlers for the orca_auto_flow Telegram bot."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orca_auto.activity_presenter import (
    QueueListPresentationDeps,
    QueueListPresentationRequest,
    queue_list_text_presentation,
)
from orca_auto.activity_rendering import queue_clear_lines, queue_list_text_lines
from orca_auto.activity_view import (
    activity_counter_config_path,
    count_global_active_simulations,
    filter_activity_items,
    queue_list_default_visible_items,
    queue_list_display_rows,
)
from orca_auto.core.activity_icons import activity_status_icon
from orca_auto.core.notifications import escape_html

from .activity import cancel_activity, clear_activities, list_activities
from .telegram_settings import TelegramBotSettings


def status_icon(
    status: str,
    *,
    activity_status_icon_fn: Callable[[str], str] = activity_status_icon,
) -> str:
    return activity_status_icon_fn(status)


def activity_payload(
    settings: TelegramBotSettings,
    *,
    child_job_engines: tuple[str, ...] | None = None,
    list_activities_fn: Callable[..., dict[str, Any]] = list_activities,
) -> dict[str, Any]:
    return list_activities_fn(
        workflow_root=settings.workflow_root,
        crest_config=settings.crest_config,
        xtb_config=settings.xtb_config,
        orca_config=settings.orca_config,
        child_job_engines=child_job_engines,
    )


def activity_counter_config_path_for_payload(
    payload: dict[str, Any],
    *,
    settings: TelegramBotSettings,
    activity_counter_config_path_fn: Callable[..., str | None] = activity_counter_config_path,
) -> str | None:
    return activity_counter_config_path_fn(
        payload,
        config_hints=(settings.orca_config, settings.crest_config, settings.xtb_config),
    )


def _handle_list_clear(
    settings: TelegramBotSettings,
    *,
    clear_activities_fn: Callable[..., dict[str, Any]],
    queue_clear_lines_fn: Callable[[dict[str, Any]], list[str]],
) -> str:
    payload = clear_activities_fn(
        workflow_root=settings.workflow_root,
        crest_config=settings.crest_config,
        xtb_config=settings.xtb_config,
        orca_config=settings.orca_config,
    )
    return "\n".join(queue_clear_lines_fn(payload))


def _list_payload_for_filter(
    settings: TelegramBotSettings,
    *,
    filter_status: str,
    activity_payload_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    return activity_payload_fn(
        settings,
        child_job_engines=() if not filter_status else None,
    )


def _list_visible_rows(
    payload: dict[str, Any],
    *,
    filter_status: str,
    filter_activity_items_fn: Callable[..., list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    all_rows = list(payload.get("activities", []))
    if not filter_status:
        return list(all_rows)
    return filter_activity_items_fn(all_rows, statuses=(filter_status,))


def _list_presentation_request(
    settings: TelegramBotSettings,
    *,
    rows: list[dict[str, Any]],
    filter_status: str,
) -> QueueListPresentationRequest:
    return QueueListPresentationRequest(
        visible_items=rows,
        config_hints=(settings.orca_config, settings.crest_config, settings.xtb_config),
        default_visible_items=not filter_status,
        show_workflow_context=True,
        visible_workflow_child_engines=("orca",) if not filter_status else None,
        include_id=False,
    )


def _list_presentation_deps(
    *,
    activity_counter_config_path_fn: Callable[..., str | None],
    count_global_active_simulations_fn: Callable[..., int],
    queue_list_default_visible_items_fn: Callable[..., list[dict[str, Any]]],
    queue_list_display_rows_fn: Callable[..., list[tuple[int, dict[str, Any]]]],
    queue_list_text_lines_fn: Callable[..., list[str]],
) -> QueueListPresentationDeps:
    return QueueListPresentationDeps(
        activity_counter_config_path=activity_counter_config_path_fn,
        count_global_active_simulations=count_global_active_simulations_fn,
        queue_list_default_visible_items=queue_list_default_visible_items_fn,
        queue_list_display_rows=queue_list_display_rows_fn,
        queue_list_text_lines=queue_list_text_lines_fn,
    )


def _handle_list_display(
    settings: TelegramBotSettings,
    *,
    filter_status: str,
    activity_payload_fn: Callable[..., dict[str, Any]],
    filter_activity_items_fn: Callable[..., list[dict[str, Any]]],
    queue_list_text_presentation_fn: Callable[..., Any],
    activity_counter_config_path_fn: Callable[..., str | None],
    count_global_active_simulations_fn: Callable[..., int],
    queue_list_default_visible_items_fn: Callable[..., list[dict[str, Any]]],
    queue_list_display_rows_fn: Callable[..., list[tuple[int, dict[str, Any]]]],
    queue_list_text_lines_fn: Callable[..., list[str]],
) -> str:
    payload = _list_payload_for_filter(
        settings,
        filter_status=filter_status,
        activity_payload_fn=activity_payload_fn,
    )
    rows = _list_visible_rows(
        payload,
        filter_status=filter_status,
        filter_activity_items_fn=filter_activity_items_fn,
    )
    presentation = queue_list_text_presentation_fn(
        payload,
        request=_list_presentation_request(
            settings,
            rows=rows,
            filter_status=filter_status,
        ),
        deps=_list_presentation_deps(
            activity_counter_config_path_fn=activity_counter_config_path_fn,
            count_global_active_simulations_fn=count_global_active_simulations_fn,
            queue_list_default_visible_items_fn=queue_list_default_visible_items_fn,
            queue_list_display_rows_fn=queue_list_display_rows_fn,
            queue_list_text_lines_fn=queue_list_text_lines_fn,
        ),
    )
    return "\n".join(presentation.lines)


def handle_list(
    settings: TelegramBotSettings,
    args: str,
    *,
    activity_payload_fn: Callable[..., dict[str, Any]] = activity_payload,
    clear_activities_fn: Callable[..., dict[str, Any]] = clear_activities,
    queue_clear_lines_fn: Callable[[dict[str, Any]], list[str]] = queue_clear_lines,
    filter_activity_items_fn: Callable[..., list[dict[str, Any]]] = filter_activity_items,
    queue_list_text_presentation_fn: Callable[..., Any] = queue_list_text_presentation,
    activity_counter_config_path_fn: Callable[..., str | None] = activity_counter_config_path,
    count_global_active_simulations_fn: Callable[..., int] = count_global_active_simulations,
    queue_list_default_visible_items_fn: Callable[..., list[dict[str, Any]]] = (
        queue_list_default_visible_items
    ),
    queue_list_display_rows_fn: Callable[..., list[tuple[int, dict[str, Any]]]] = (
        queue_list_display_rows
    ),
    queue_list_text_lines_fn: Callable[..., list[str]] = queue_list_text_lines,
) -> str:
    action = args.strip().lower()
    if action == "clear":
        return _handle_list_clear(
            settings,
            clear_activities_fn=clear_activities_fn,
            queue_clear_lines_fn=queue_clear_lines_fn,
        )

    return _handle_list_display(
        settings,
        filter_status=action,
        activity_payload_fn=activity_payload_fn,
        filter_activity_items_fn=filter_activity_items_fn,
        queue_list_text_presentation_fn=queue_list_text_presentation_fn,
        activity_counter_config_path_fn=activity_counter_config_path_fn,
        count_global_active_simulations_fn=count_global_active_simulations_fn,
        queue_list_default_visible_items_fn=queue_list_default_visible_items_fn,
        queue_list_display_rows_fn=queue_list_display_rows_fn,
        queue_list_text_lines_fn=queue_list_text_lines_fn,
    )


def handle_cancel(
    settings: TelegramBotSettings,
    args: str,
    *,
    cancel_activity_fn: Callable[..., dict[str, Any]] = cancel_activity,
    escape_html_fn: Callable[[str], str] = escape_html,
    status_icon_fn: Callable[[str], str] = status_icon,
) -> str:
    target = args.strip()
    if not target:
        return "Usage: /cancel &lt;target&gt;"
    try:
        payload = cancel_activity_fn(
            target=target,
            workflow_root=settings.workflow_root,
            crest_config=settings.crest_config,
            xtb_config=settings.xtb_config,
            orca_config=settings.orca_config,
            orca_repo_root=settings.orca_repo_root,
        )
    except (LookupError, ValueError) as exc:
        return escape_html_fn(str(exc))

    label = escape_html_fn(str(payload.get("label", payload.get("activity_id", target))))
    status = escape_html_fn(str(payload.get("status", "unknown")))
    return f"{status_icon_fn(status)} <b>{label}</b>\nstatus: <code>{status}</code>"


def handle_help(_settings: TelegramBotSettings, _args: str) -> str:
    return (
        "<b>orca_auto_flow bot commands</b>\n\n"
        "/list — Show unified activities\n"
        "/list clear — Remove completed/failed/cancelled entries\n"
        "/list running — Running activities only\n"
        "/list failed — Failed activities only\n"
        "/cancel &lt;target&gt; — Cancel a workflow or queued job (asks to confirm)\n"
        "/help — This help message"
    )


__all__ = [
    "activity_counter_config_path_for_payload",
    "activity_payload",
    "handle_cancel",
    "handle_help",
    "handle_list",
    "status_icon",
]
