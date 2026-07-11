"""Messenger-neutral notification layer.

Domain code builds a :class:`Message` (see :mod:`.richtext`) and sends it through
a :class:`MessageChannel` resolved by :func:`build_channel`. Per-messenger markup
and transport live in the adapter modules (Telegram, Discord) so the active
messenger can be swapped from config alone.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .channel import MessageChannel, SendResult, send_ok
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
    field_row,
    group,
    line,
    raw,
    text,
    title_heading,
)

if TYPE_CHECKING:
    from .config_io import build_channel_from_config_path, load_messenger_config_from_file
    from .discord_webhook import DiscordWebhookChannel
    from .registry import build_channel
    from .telegram_channel import TelegramChannel

_LAZY_EXPORTS = {
    "DiscordWebhookChannel": (".discord_webhook", "DiscordWebhookChannel"),
    "TelegramChannel": (".telegram_channel", "TelegramChannel"),
    "build_channel": (".registry", "build_channel"),
    "build_channel_from_config_path": (".config_io", "build_channel_from_config_path"),
    "load_messenger_config_from_file": (".config_io", "load_messenger_config_from_file"),
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
    "DiscordWebhookChannel",
    "Field",
    "Group",
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
    "load_messenger_config_from_file",
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
