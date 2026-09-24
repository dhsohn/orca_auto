"""Messenger-neutral notification contracts (Discord outbound).

Domain code builds a :class:`Message` (see :mod:`.richtext`) and sends it through
the :class:`MessageChannel` returned by :func:`build_channel`: the Discord bot
adapter when its config is complete, otherwise a null channel that skips every
send. Discord markup and transport live in the adapter modules.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .channel import DisabledChannel, MessageChannel, SendResult, build_channel
from .render_discord import render_discord_embed
from .richtext import (
    Field,
    Group,
    Message,
    Severity,
    Span,
    code,
    field_row,
    group,
    raw,
    text,
)

if TYPE_CHECKING:
    from .discord_bot import DiscordBotChannel

_LAZY_EXPORTS = {
    "DiscordBotChannel": (".discord_bot", "DiscordBotChannel"),
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
    "DisabledChannel",
    "DiscordBotChannel",
    "Field",
    "Group",
    "Message",
    "MessageChannel",
    "SendResult",
    "Severity",
    "Span",
    "build_channel",
    "code",
    "field_row",
    "group",
    "raw",
    "render_discord_embed",
    "text",
]
