"""Messenger-neutral notification contracts (outbound only: Discord or Slack).

Domain code builds a :class:`Message` (see :mod:`.richtext`) and sends it through
the :class:`MessageChannel` returned by :func:`build_channel`: the adapter of the
selected provider (Discord by default, or Slack) when its config is complete,
otherwise a null channel that skips every send. Provider markup and transport
live in the adapter modules, which are imported only when selected.
"""

from __future__ import annotations

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

__all__ = [
    "DisabledChannel",
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
