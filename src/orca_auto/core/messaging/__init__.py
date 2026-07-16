"""Messenger-neutral notification and interactive contracts.

Domain code builds a :class:`Message` (see :mod:`.richtext`) and sends it through
a :class:`MessageChannel` resolved by :func:`build_channel`. Per-messenger markup
and transport live in the adapter modules (Telegram, Discord) so the active
messenger can be swapped from config alone. Interactive bot applications use
the neutral values and port exported from :mod:`.interactive`.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .channel import MessageChannel, SendResult, send_ok
from .interactive import (
    ActionRows,
    Actor,
    BotReply,
    BotReplyFormat,
    CardAction,
    ConversationAddress,
    IncomingAction,
    IncomingCommand,
    InteractiveMessenger,
)
from .render_discord import render_discord_embed
from .render_telegram import render_telegram
from .richtext import (
    Field,
    Group,
    Line,
    Message,
    Severity,
    Span,
    bold,
    code,
    code_block,
    field_row,
    group,
    line,
    raw,
    text,
    title_heading,
)
from .telegram_api import TelegramApiClient
from .telegram_config import (
    DEFAULT_TELEGRAM_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    load_telegram_config_from_file,
)
from .telegram_format import (
    MAX_TELEGRAM_MESSAGE_LENGTH,
    escape_html,
    escape_html_text,
    html_code,
    split_telegram_message,
)
from .telegram_transport import (
    TelegramDeliveryResult,
    TelegramSendResult,
    TelegramTransport,
    build_telegram_transport,
    log_telegram_send_failure,
    send_rendered_telegram_chunks,
    telegram_send_result_ok,
)

if TYPE_CHECKING:
    from .config_io import (
        build_channel_from_config_path,
        load_messenger_config_from_file,
        load_required_messenger_config_from_file,
    )
    from .discord_bot import DiscordBotChannel
    from .registry import build_channel
    from .telegram_channel import TelegramChannel

_LAZY_EXPORTS = {
    "DiscordBotChannel": (".discord_bot", "DiscordBotChannel"),
    "TelegramChannel": (".telegram_channel", "TelegramChannel"),
    "build_channel": (".registry", "build_channel"),
    "build_channel_from_config_path": (".config_io", "build_channel_from_config_path"),
    "load_messenger_config_from_file": (".config_io", "load_messenger_config_from_file"),
    "load_required_messenger_config_from_file": (
        ".config_io",
        "load_required_messenger_config_from_file",
    ),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


__all__ = [
    "DEFAULT_TELEGRAM_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TELEGRAM_MESSAGE_LENGTH",
    "TelegramApiClient",
    "TelegramDeliveryResult",
    "TelegramSendResult",
    "TelegramTransport",
    "build_telegram_transport",
    "escape_html",
    "escape_html_text",
    "html_code",
    "load_telegram_config_from_file",
    "log_telegram_send_failure",
    "send_rendered_telegram_chunks",
    "split_telegram_message",
    "telegram_send_result_ok",
    "ActionRows",
    "Actor",
    "BotReply",
    "BotReplyFormat",
    "CardAction",
    "ConversationAddress",
    "DiscordBotChannel",
    "Field",
    "Group",
    "IncomingAction",
    "IncomingCommand",
    "InteractiveMessenger",
    "Line",
    "Message",
    "MessageChannel",
    "SendResult",
    "Severity",
    "Span",
    "TelegramChannel",
    "bold",
    "build_channel",
    "build_channel_from_config_path",
    "code",
    "code_block",
    "load_messenger_config_from_file",
    "load_required_messenger_config_from_file",
    "field_row",
    "group",
    "line",
    "raw",
    "render_discord_embed",
    "render_telegram",
    "send_ok",
    "text",
    "title_heading",
]
