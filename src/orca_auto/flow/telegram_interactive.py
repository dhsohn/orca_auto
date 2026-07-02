"""Interactive Telegram callback helpers for orca_auto_flow."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from orca_auto.activity_view import queue_list_default_visible_items
from orca_auto.core.notifications import escape_html
from orca_auto.core.statuses import QUEUE_ACTIVE_STATUSES

from .telegram_keyboards import (
    _CB_CANCEL_ASK,
    _CB_CANCEL_DO,
    _CB_CANCEL_NO,
    _CB_CLEAR,
    _CB_REFRESH,
    _MAX_LIST_CANCEL_BUTTONS,
    _cancel_confirm_keyboard,
    _list_action_keyboard,
)

ActivityPayloadFn = Callable[..., dict[str, Any]]


def send_cancel_confirmation(
    settings: Any,
    target: str,
    *,
    send_response: Callable[..., Any],
    send_message: Callable[..., Any],
    handle_cancel: Callable[[Any, str], str],
) -> None:
    target = target.strip()
    if not target:
        send_response(settings.telegram, "Usage: /cancel &lt;target&gt;")
        return
    keyboard = _cancel_confirm_keyboard(target)
    if keyboard is None:
        # Identifier too long for an inline button; cancel directly.
        send_response(settings.telegram, handle_cancel(settings, target))
        return
    send_message(
        settings,
        f"⚠️ Cancel <code>{escape_html(target)}</code>?",
        reply_markup=keyboard,
    )


def callback_response(
    settings: Any,
    data: str,
    *,
    handle_cancel: Callable[[Any, str], str],
) -> str:
    if data == _CB_CANCEL_NO:
        return "✖ Cancellation dismissed."
    if data.startswith(_CB_CANCEL_DO):
        return handle_cancel(settings, data[len(_CB_CANCEL_DO) :])
    return "Unknown action."


def active_cancel_targets(
    settings: Any,
    *,
    activity_payload: ActivityPayloadFn,
) -> list[dict[str, Any]]:
    payload = activity_payload(settings, child_job_engines=("orca",))
    items = [item for item in payload.get("activities", []) if isinstance(item, dict)]
    visible = queue_list_default_visible_items(items)
    return [
        item
        for item in visible
        if str(item.get("status", "")).strip().lower() in QUEUE_ACTIVE_STATUSES
    ]


def send_list_actions(
    settings: Any,
    *,
    active_cancel_targets_fn: Callable[[Any], list[dict[str, Any]]],
    send_message: Callable[..., Any],
    logger: logging.Logger,
) -> None:
    """Send a short action message with per-activity cancel + refresh buttons."""

    try:
        active = active_cancel_targets_fn(settings)
        total = len(active)
        if total > _MAX_LIST_CANCEL_BUTTONS:
            text = f"🔧 Actions (showing {_MAX_LIST_CANCEL_BUTTONS} of {total} cancellable):"
        else:
            text = "🔧 Actions:"
        send_message(settings, text, reply_markup=_list_action_keyboard(active))
    except Exception:
        logger.exception("telegram_bot_list_actions_error")


def send_list_response(
    settings: Any,
    *,
    send_preformatted_response: Callable[..., Any],
    handle_list: Callable[[Any, str], str],
    send_list_actions_fn: Callable[[Any], None],
) -> None:
    send_preformatted_response(settings.telegram, handle_list(settings, ""))
    send_list_actions_fn(settings)


def dispatch_callback_query(
    settings: Any,
    update: dict[str, Any],
    *,
    answer_callback: Callable[[Any, Any], Any],
    send_list_response_fn: Callable[[Any], None],
    send_cancel_confirmation_fn: Callable[[Any, str], None],
    callback_response_fn: Callable[[Any, str], str],
    edit_message: Callable[..., Any],
    send_response: Callable[..., Any],
    clear_finished_fn: Callable[[Any], None],
) -> int | None:
    update_id = int(update.get("update_id", 0) or 0)
    callback = update.get("callback_query")
    if not isinstance(callback, dict):
        return update_id

    message = callback.get("message")
    message = message if isinstance(message, dict) else {}
    chat = message.get("chat")
    chat = chat if isinstance(chat, dict) else {}
    if str(chat.get("id", "")).strip() != settings.telegram.chat_id:
        answer_callback(settings, callback.get("id"))
        return update_id

    data = str(callback.get("data") or "")
    answer_callback(settings, callback.get("id"))

    if data == _CB_REFRESH:
        send_list_response_fn(settings)
        return update_id
    if data == _CB_CLEAR:
        # Same effect as ``/list clear``: prune finished entries, report the
        # result, then refresh so the list and action buttons reflect it.
        clear_finished_fn(settings)
        send_list_response_fn(settings)
        return update_id
    if data.startswith(_CB_CANCEL_ASK):
        send_cancel_confirmation_fn(settings, data[len(_CB_CANCEL_ASK) :])
        return update_id

    response = callback_response_fn(settings, data)
    message_id = message.get("message_id")
    if message_id is not None:
        edit_message(settings, chat_id=chat.get("id"), message_id=message_id, text=response)
    else:
        send_response(settings.telegram, response)
    if data.startswith(_CB_CANCEL_DO):
        # A cancellation was just executed; refresh the list so the actions
        # message reflects the new state.
        send_list_response_fn(settings)
    return update_id
