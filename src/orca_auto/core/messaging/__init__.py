"""Messenger-neutral notification contracts (Discord outbound).

Domain code builds a :class:`Message` (see :mod:`.richtext`) and sends it through
a :class:`MessageChannel` resolved by :func:`build_channel`. Discord markup and
transport live in the adapter modules so the channel is resolved from config.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .channel import MessageChannel, SendResult
from .render_discord import render_discord_embed
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
    from .config_io import (
        build_channel_from_config_path,
        load_messenger_config_from_file,
        load_required_messenger_config_from_file,
    )
    from .discord_bot import DiscordBotChannel
    from .registry import build_channel

_LAZY_EXPORTS = {
    "DiscordBotChannel": (".discord_bot", "DiscordBotChannel"),
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
    "DiscordBotChannel",
    "Field",
    "Group",
    "Line",
    "Message",
    "MessageChannel",
    "SendResult",
    "Severity",
    "Span",
    "bold",
    "build_channel",
    "build_channel_from_config_path",
    "code",
    "field_row",
    "group",
    "line",
    "load_messenger_config_from_file",
    "load_required_messenger_config_from_file",
    "raw",
    "render_discord_embed",
    "text",
    "title_heading",
]
