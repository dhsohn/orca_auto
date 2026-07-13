"""Rich-message builders for interactive bot replies.

These wrap the provider-neutral :mod:`orca_auto.core.messaging` document model so
every interactive reply shares the notification look: an ``orca_auto`` author
line, a severity-driven colour and glyph, and a clean title. Each ``BotReply``
keeps its plain ``text`` as the fallback; the ``message`` built here is what the
Discord and Telegram adapters render natively.
"""

from __future__ import annotations

from orca_auto.core.messaging import (
    Message,
    code,
    code_block,
    field_row,
    group,
    line,
    raw,
    text,
)
from orca_auto.core.messaging.richtext import Item, Severity

_AUTHOR = "orca_auto"


def reply_message(title: str, *items: Item, severity: Severity = "info") -> Message:
    """Build an interactive-reply document with the shared author identity."""
    return Message(
        title=title,
        severity=severity,
        author=_AUTHOR,
        groups=(group(*items),) if items else (),
    )


def error_message(title: str, detail: str = "") -> Message:
    """An error reply: red embed / warning colour with an optional detail line."""
    items = (line(text(detail)),) if detail.strip() else ()
    return reply_message(title, *items, severity="error")


def info_message(title: str, detail: str = "") -> Message:
    """A neutral reply built from plain strings (no span builders at the call site)."""
    items = (line(text(detail)),) if detail.strip() else ()
    return reply_message(title, *items, severity="info")


__all__ = [
    "code",
    "code_block",
    "error_message",
    "field_row",
    "info_message",
    "line",
    "raw",
    "reply_message",
    "text",
]
