"""Command dispatch and polling helpers for the orca_auto_flow Telegram bot."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from orca_auto.core.notifications import escape_html

from . import telegram_interactive as _interactive
from .telegram_bot_api import POLL_TIMEOUT_SECONDS
from .telegram_settings import TelegramBotSettings, settings_from_env


def set_bot_commands(token: str, *, api_call: Callable[..., Any]) -> None:
    commands = [
        {"command": "list", "description": "Show unified activity list"},
        {"command": "cancel", "description": "Cancel a workflow or job"},
        {"command": "help", "description": "Help"},
    ]
    api_call(token, "setMyCommands", {"commands": commands})


def send_message(
    settings: TelegramBotSettings,
    text: str,
    *,
    api_call: Callable[..., Any],
    parse_mode: str | None = "HTML",
    reply_markup: dict[str, Any] | None = None,
) -> Any | None:
    payload: dict[str, Any] = {"chat_id": settings.telegram.chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return api_call(settings.telegram.bot_token, "sendMessage", payload)


def edit_message(
    settings: TelegramBotSettings,
    *,
    chat_id: Any,
    message_id: Any,
    text: str,
    api_call: Callable[..., Any],
) -> Any | None:
    return api_call(
        settings.telegram.bot_token,
        "editMessageText",
        {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"},
    )


def answer_callback(
    settings: TelegramBotSettings,
    callback_id: Any,
    *,
    api_call: Callable[..., Any],
) -> Any | None:
    return api_call(
        settings.telegram.bot_token,
        "answerCallbackQuery",
        {"callback_query_id": callback_id},
    )


def send_cancel_confirmation(
    settings: TelegramBotSettings,
    target: str,
    *,
    send_response: Callable[..., Any],
    send_message_fn: Callable[..., Any],
    handle_cancel: Callable[[TelegramBotSettings, str], str],
) -> None:
    _interactive.send_cancel_confirmation(
        settings,
        target,
        send_response=send_response,
        send_message=send_message_fn,
        handle_cancel=handle_cancel,
    )


def callback_response(
    settings: TelegramBotSettings,
    data: str,
    *,
    handle_cancel: Callable[[TelegramBotSettings, str], str],
) -> str:
    return _interactive.callback_response(settings, data, handle_cancel=handle_cancel)


def active_cancel_targets(
    settings: TelegramBotSettings,
    *,
    activity_payload: Callable[..., dict[str, Any]],
) -> list[dict[str, Any]]:
    return _interactive.active_cancel_targets(settings, activity_payload=activity_payload)


def send_list_actions(
    settings: TelegramBotSettings,
    *,
    active_cancel_targets_fn: Callable[[TelegramBotSettings], list[dict[str, Any]]],
    send_message_fn: Callable[..., Any],
    logger: logging.Logger,
) -> None:
    _interactive.send_list_actions(
        settings,
        active_cancel_targets_fn=active_cancel_targets_fn,
        send_message=send_message_fn,
        logger=logger,
    )


def send_list_response(
    settings: TelegramBotSettings,
    *,
    send_preformatted_response: Callable[..., Any],
    handle_list: Callable[[TelegramBotSettings, str], str],
    send_list_actions_fn: Callable[[TelegramBotSettings], None],
) -> None:
    _interactive.send_list_response(
        settings,
        send_preformatted_response=send_preformatted_response,
        handle_list=handle_list,
        send_list_actions_fn=send_list_actions_fn,
    )


def poll_updates(
    token: str,
    offset: int,
    *,
    api_call: Callable[..., Any],
    poll_timeout_seconds: int = POLL_TIMEOUT_SECONDS,
) -> list[Any]:
    updates = api_call(
        token,
        "getUpdates",
        {
            "offset": offset,
            "timeout": poll_timeout_seconds,
            "allowed_updates": ["message", "callback_query"],
        },
        timeout=poll_timeout_seconds + 5,
    )
    return updates if isinstance(updates, list) else []


def message_from_update(
    update: Any,
    *,
    chat_id: str,
) -> tuple[int | None, dict[str, Any] | None]:
    if not isinstance(update, dict):
        return None, None

    update_id = int(update.get("update_id", 0) or 0)
    message = update.get("message")
    if not isinstance(message, dict):
        return update_id, None

    chat = message.get("chat")
    chat_dict = chat if isinstance(chat, dict) else {}
    if str(chat_dict.get("id", "")).strip() != chat_id:
        return update_id, None
    return update_id, message


def command_from_message(message: dict[str, Any]) -> tuple[str, str] | None:
    text_value = message.get("text")
    text = text_value.strip() if isinstance(text_value, str) else ""
    if not text.startswith("/"):
        return None

    parts = text.split(maxsplit=1)
    command = parts[0].lstrip("/").split("@")[0].lower()
    return command, parts[1] if len(parts) > 1 else ""


def response_for_command(
    settings: TelegramBotSettings,
    command: str,
    args: str,
    *,
    handlers: dict[str, Callable[[TelegramBotSettings, str], str]],
    escape_html_fn: Callable[[str], str] = escape_html,
    logger: logging.Logger,
) -> tuple[str, bool]:
    handler = handlers.get(command)
    if handler is None:
        return (
            f"Unknown command: /{escape_html_fn(command)}\nType /help for available commands.",
            False,
        )

    try:
        return handler(settings, args), command == "list"
    except Exception as exc:
        logger.exception("telegram_bot_handler_error: cmd=%s", command)
        return f"Error: {escape_html_fn(str(exc))}", False


def send_bot_response(
    settings: TelegramBotSettings,
    response: str,
    *,
    preformatted: bool,
    send_preformatted_response: Callable[..., Any],
    send_response: Callable[..., Any],
) -> None:
    if preformatted:
        send_preformatted_response(settings.telegram, response)
    else:
        send_response(settings.telegram, response)


def dispatch_callback_query(
    settings: TelegramBotSettings,
    update: dict[str, Any],
    *,
    answer_callback_fn: Callable[[TelegramBotSettings, Any], Any],
    send_list_response_fn: Callable[[TelegramBotSettings], None],
    send_cancel_confirmation_fn: Callable[[TelegramBotSettings, str], None],
    callback_response_fn: Callable[[TelegramBotSettings, str], str],
    edit_message_fn: Callable[..., Any],
    send_response: Callable[..., Any],
    clear_finished_fn: Callable[[TelegramBotSettings], None],
) -> int | None:
    return _interactive.dispatch_callback_query(
        settings,
        update,
        answer_callback=answer_callback_fn,
        send_list_response_fn=send_list_response_fn,
        send_cancel_confirmation_fn=send_cancel_confirmation_fn,
        callback_response_fn=callback_response_fn,
        edit_message=edit_message_fn,
        send_response=send_response,
        clear_finished_fn=clear_finished_fn,
    )


def dispatch_update(
    settings: TelegramBotSettings,
    update: Any,
    *,
    dispatch_callback_query_fn: Callable[[TelegramBotSettings, dict[str, Any]], int | None],
    message_from_update_fn: Callable[..., tuple[int | None, dict[str, Any] | None]],
    command_from_message_fn: Callable[[dict[str, Any]], tuple[str, str] | None],
    send_cancel_confirmation_fn: Callable[[TelegramBotSettings, str], None],
    response_for_command_fn: Callable[[TelegramBotSettings, str, str], tuple[str, bool]],
    send_bot_response_fn: Callable[..., None],
    send_list_actions_fn: Callable[[TelegramBotSettings], None],
) -> int | None:
    if isinstance(update, dict) and isinstance(update.get("callback_query"), dict):
        return dispatch_callback_query_fn(settings, update)

    update_id, message = message_from_update_fn(update, chat_id=settings.telegram.chat_id)
    if message is None:
        return update_id

    command_parts = command_from_message_fn(message)
    if command_parts is None:
        return update_id

    command, args = command_parts
    if command == "cancel":
        send_cancel_confirmation_fn(settings, args)
        return update_id

    response, preformatted = response_for_command_fn(settings, command, args)
    send_bot_response_fn(settings, response, preformatted=preformatted)
    if command == "list" and args.strip().lower() != "clear":
        send_list_actions_fn(settings)
    return update_id


def run_bot(
    settings: TelegramBotSettings | None = None,
    *,
    settings_from_env_fn: Callable[[], TelegramBotSettings] = settings_from_env,
    set_bot_commands_fn: Callable[[str], None],
    poll_updates_fn: Callable[[str, int], list[Any]],
    dispatch_update_fn: Callable[[TelegramBotSettings, Any], int | None],
    logger: logging.Logger,
    sleep: Callable[[float], Any] = time.sleep,
) -> int:
    resolved = settings or settings_from_env_fn()
    if not resolved.enabled:
        logger.error(
            "Telegram is not configured. Set telegram.bot_token/chat_id in orca_auto.yaml "
            "or ORCA_AUTO_FLOW_TELEGRAM_BOT_TOKEN and ORCA_AUTO_FLOW_TELEGRAM_CHAT_ID."
        )
        return 1

    set_bot_commands_fn(resolved.telegram.bot_token)
    logger.info("orca_auto_flow Telegram bot started (chat_id=%s)", resolved.telegram.chat_id)

    offset = 0
    while True:
        try:
            for update in poll_updates_fn(resolved.telegram.bot_token, offset):
                update_id = dispatch_update_fn(resolved, update)
                if update_id is not None:
                    offset = max(offset, update_id + 1)
        except KeyboardInterrupt:
            logger.info("orca_auto_flow Telegram bot stopped")
            return 0
        except Exception as exc:
            logger.exception("telegram_bot_poll_error: %s", exc)
            sleep(5)


__all__ = [
    "active_cancel_targets",
    "answer_callback",
    "callback_response",
    "command_from_message",
    "dispatch_callback_query",
    "dispatch_update",
    "edit_message",
    "message_from_update",
    "poll_updates",
    "response_for_command",
    "run_bot",
    "send_bot_response",
    "send_cancel_confirmation",
    "send_list_actions",
    "send_list_response",
    "send_message",
    "set_bot_commands",
]
