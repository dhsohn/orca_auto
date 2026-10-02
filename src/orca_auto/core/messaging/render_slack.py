"""Render a :class:`~orca_auto.core.messaging.richtext.Message` as Slack plain text.

The text is sent with Markdown off. Slack still reads ``&``, ``<`` and ``>`` as
control characters (``<!channel>`` mentions, ``<url|label>`` links), so they are
escaped and nothing in a message can mention or link. The rendering is bounded
well below Slack's message-text limit.
"""

from __future__ import annotations

from .richtext import Field, Message

_MAX_TEXT_CHARS = 3000
_TRUNCATION_MARK = "…"


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _field_line(item: Field) -> str:
    value = "".join(span.text for span in item.value)
    return f"{_escape(item.label)}: {_escape(value)}"


def render_slack_text(message: Message) -> str:
    """Return the plain-text Slack body for ``message``."""

    headline = _escape(message.title)
    if message.author:
        headline = f"{_escape(message.author)}: {headline}"
    lines = [headline]
    for group in message.groups:
        lines.extend(_field_line(item) for item in group.items)
    rendered = "\n".join(lines)
    if len(rendered) > _MAX_TEXT_CHARS:
        rendered = rendered[: _MAX_TEXT_CHARS - len(_TRUNCATION_MARK)] + _TRUNCATION_MARK
    return rendered


__all__ = ["render_slack_text"]
