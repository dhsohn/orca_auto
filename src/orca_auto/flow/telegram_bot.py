"""Telegram bot facade for unified orca_auto_flow activity control."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.activity_presenter import queue_list_text_presentation
from orca_auto.activity_rendering import queue_clear_lines, queue_list_text_lines
from orca_auto.activity_view import (
    activity_counter_config_path,
    count_global_active_simulations,
    filter_activity_items,
    queue_list_default_visible_items,
    queue_list_display_rows,
)
from orca_auto.core.activity_icons import activity_status_icon
from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.config.files import config_env_value
from orca_auto.core.notifications import (
    build_telegram_transport,
    escape_html,
    load_telegram_config_from_file,
)

from . import _activity_sources
from . import telegram_bot_api as _api
from . import telegram_commands as _commands
from . import telegram_dispatch as _dispatch
from . import telegram_settings as _settings
from .activity import cancel_activity, clear_activities, list_activities
from .telegram_keyboards import (
    _MAX_LIST_CANCEL_BUTTONS as _MAX_LIST_CANCEL_BUTTONS,
)
from .telegram_keyboards import (
    _cancel_confirm_keyboard as _cancel_confirm_keyboard,
)
from .telegram_keyboards import (
    _list_action_keyboard as _list_action_keyboard,
)

logger = logging.getLogger(__name__)

TelegramBotSettings = _settings.TelegramBotSettings
_API_BASE = _api.API_BASE
_POLL_TIMEOUT_SECONDS = _api.POLL_TIMEOUT_SECONDS
_MAX_MESSAGE_LENGTH = _api.MAX_MESSAGE_LENGTH


def settings_from_env() -> TelegramBotSettings:
    return _settings.settings_from_env(
        activity_sources=_activity_sources,
        getenv=os.getenv,
    )


def _telegram_from_config_path(config_path: str | None) -> Any:
    return _settings.telegram_from_config_path(
        config_path,
        path_cls=Path,
        load_telegram_config=load_telegram_config_from_file,
    )


def settings_from_config(config_path: str | None = None) -> TelegramBotSettings:
    return _settings.settings_from_config(
        config_path,
        activity_sources=_activity_sources,
        getenv=os.getenv,
        path_cls=Path,
        load_telegram_config=_telegram_from_config_path,
    )


def _api_call(
    token: str,
    method: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: int = _POLL_TIMEOUT_SECONDS + 5,
) -> Any | None:
    return _api.api_call(
        token,
        method,
        payload,
        timeout=timeout,
        api_base=_API_BASE,
        logger=logger,
    )


def _send_response(
    config: Any,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    limit: int = _MAX_MESSAGE_LENGTH,
) -> bool:
    return _api.send_response(
        config,
        text,
        parse_mode=parse_mode,
        limit=limit,
        logger=logger,
        transport_factory=build_telegram_transport,
    )


def _send_preformatted_response(
    config: Any,
    text: str,
    *,
    limit: int = _MAX_MESSAGE_LENGTH,
) -> bool:
    return _api.send_preformatted_response(
        config,
        text,
        limit=limit,
        logger=logger,
        transport_factory=build_telegram_transport,
    )


def _status_icon(status: str) -> str:
    return _commands.status_icon(status, activity_status_icon_fn=activity_status_icon)


def _activity_payload(
    settings: TelegramBotSettings,
    *,
    child_job_engines: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    return _commands.activity_payload(
        settings,
        child_job_engines=child_job_engines,
        list_activities_fn=list_activities,
    )


def _activity_counter_config_path(
    payload: dict[str, Any],
    *,
    settings: TelegramBotSettings,
) -> str | None:
    return _commands.activity_counter_config_path_for_payload(
        payload,
        settings=settings,
        activity_counter_config_path_fn=activity_counter_config_path,
    )


def _handle_list(settings: TelegramBotSettings, args: str) -> str:
    return _commands.handle_list(
        settings,
        args,
        activity_payload_fn=_activity_payload,
        clear_activities_fn=clear_activities,
        queue_clear_lines_fn=queue_clear_lines,
        filter_activity_items_fn=filter_activity_items,
        queue_list_text_presentation_fn=queue_list_text_presentation,
        activity_counter_config_path_fn=activity_counter_config_path,
        count_global_active_simulations_fn=count_global_active_simulations,
        queue_list_default_visible_items_fn=queue_list_default_visible_items,
        queue_list_display_rows_fn=queue_list_display_rows,
        queue_list_text_lines_fn=queue_list_text_lines,
    )


def _handle_cancel(settings: TelegramBotSettings, args: str) -> str:
    return _commands.handle_cancel(
        settings,
        args,
        cancel_activity_fn=cancel_activity,
        escape_html_fn=escape_html,
        status_icon_fn=_status_icon,
    )


def _handle_help(settings: TelegramBotSettings, args: str) -> str:
    return _commands.handle_help(settings, args)


_HANDLERS: dict[str, Callable[[TelegramBotSettings, str], str]] = {
    "list": _handle_list,
    "cancel": _handle_cancel,
    "help": _handle_help,
    "start": _handle_help,
}


def _set_bot_commands(token: str) -> None:
    _dispatch.set_bot_commands(token, api_call=_api_call)


def _send_message(
    settings: TelegramBotSettings,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    reply_markup: dict[str, Any] | None = None,
) -> Any | None:
    return _dispatch.send_message(
        settings,
        text,
        api_call=_api_call,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )


def _edit_message(
    settings: TelegramBotSettings,
    *,
    chat_id: Any,
    message_id: Any,
    text: str,
) -> Any | None:
    return _dispatch.edit_message(
        settings,
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        api_call=_api_call,
    )


def _answer_callback(settings: TelegramBotSettings, callback_id: Any) -> Any | None:
    return _dispatch.answer_callback(settings, callback_id, api_call=_api_call)


def _send_cancel_confirmation(settings: TelegramBotSettings, target: str) -> None:
    _dispatch.send_cancel_confirmation(
        settings,
        target,
        send_response=_send_response,
        send_message_fn=_send_message,
        handle_cancel=_handle_cancel,
    )


def _callback_response(settings: TelegramBotSettings, data: str) -> str:
    return _dispatch.callback_response(settings, data, handle_cancel=_handle_cancel)


def _active_cancel_targets(settings: TelegramBotSettings) -> list[dict[str, Any]]:
    return _dispatch.active_cancel_targets(settings, activity_payload=_activity_payload)


def _send_list_actions(settings: TelegramBotSettings) -> None:
    _dispatch.send_list_actions(
        settings,
        active_cancel_targets_fn=_active_cancel_targets,
        send_message_fn=_send_message,
        logger=logger,
    )


def _send_list_response(settings: TelegramBotSettings) -> None:
    _dispatch.send_list_response(
        settings,
        send_preformatted_response=_send_preformatted_response,
        handle_list=_handle_list,
        send_list_actions_fn=_send_list_actions,
    )


def _poll_updates(token: str, offset: int) -> list[Any]:
    return _dispatch.poll_updates(
        token,
        offset,
        api_call=_api_call,
        poll_timeout_seconds=_POLL_TIMEOUT_SECONDS,
    )


def _message_from_update(update: Any, *, chat_id: str) -> tuple[int | None, dict[str, Any] | None]:
    return _dispatch.message_from_update(update, chat_id=chat_id)


def _command_from_message(message: dict[str, Any]) -> tuple[str, str] | None:
    return _dispatch.command_from_message(message)


def _response_for_command(
    settings: TelegramBotSettings, command: str, args: str
) -> tuple[str, bool]:
    return _dispatch.response_for_command(
        settings,
        command,
        args,
        handlers=_HANDLERS,
        escape_html_fn=escape_html,
        logger=logger,
    )


def _send_bot_response(settings: TelegramBotSettings, response: str, *, preformatted: bool) -> None:
    _dispatch.send_bot_response(
        settings,
        response,
        preformatted=preformatted,
        send_preformatted_response=_send_preformatted_response,
        send_response=_send_response,
    )


def _clear_finished(settings: TelegramBotSettings) -> None:
    _send_response(settings.telegram, _handle_list(settings, "clear"))


def _dispatch_callback_query(settings: TelegramBotSettings, update: dict[str, Any]) -> int | None:
    return _dispatch.dispatch_callback_query(
        settings,
        update,
        answer_callback_fn=_answer_callback,
        send_list_response_fn=_send_list_response,
        send_cancel_confirmation_fn=_send_cancel_confirmation,
        callback_response_fn=_callback_response,
        edit_message_fn=_edit_message,
        send_response=_send_response,
        clear_finished_fn=_clear_finished,
    )


def _dispatch_update(settings: TelegramBotSettings, update: Any) -> int | None:
    return _dispatch.dispatch_update(
        settings,
        update,
        dispatch_callback_query_fn=_dispatch_callback_query,
        message_from_update_fn=_message_from_update,
        command_from_message_fn=_command_from_message,
        send_cancel_confirmation_fn=_send_cancel_confirmation,
        response_for_command_fn=_response_for_command,
        send_bot_response_fn=_send_bot_response,
        send_list_actions_fn=_send_list_actions,
    )


def run_bot(settings: TelegramBotSettings | None = None) -> int:
    return _dispatch.run_bot(
        settings,
        settings_from_env_fn=settings_from_env,
        set_bot_commands_fn=_set_bot_commands,
        poll_updates_fn=_poll_updates,
        dispatch_update_fn=_dispatch_update,
        logger=logger,
    )


def main() -> int:
    config_path = config_env_value(ORCA_AUTO_CONFIG_ENV_VAR) or None
    return int(run_bot(settings_from_config(config_path)))


__all__ = [
    "TelegramBotSettings",
    "escape_html",
    "main",
    "run_bot",
    "settings_from_config",
    "settings_from_env",
]


if __name__ == "__main__":
    raise SystemExit(main())
