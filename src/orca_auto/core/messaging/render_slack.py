"""Render a :class:`~orca_auto.core.messaging.richtext.Message` for Slack.

Top-level ``text`` is plain with Markdown off; ``&``, ``<`` and ``>`` are escaped
so nothing in a message can mention or link. Block Kit ``blocks`` use ``plain_text``
composition objects only for dynamic labels and values (never ``mrkdwn``).
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import islice
from typing import Any

from .richtext import Field, Message

_MAX_TEXT_CHARS = 3000
_MAX_BLOCKS = 50
_HEADER_LIMIT = 150
_SECTION_TEXT_LIMIT = 3000
_MAX_FIELDS_PER_SECTION = 10
_FIELD_TEXT_LIMIT = 2000
_COMPACT_FIELD_LIMIT = 80
_TRUNCATION_MARK = "…"

_SEVERITY_EMOJI: dict[str, str] = {
    "success": "✅",
    "warning": "⚠️",
    "error": "❌",
}


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + _TRUNCATION_MARK


def _non_empty(value: str, *, limit: int) -> str:
    trimmed = _truncate(value.strip(), limit)
    return trimmed or "-"


def _plain_text(value: str, *, limit: int) -> dict[str, object]:
    return {"type": "plain_text", "text": _non_empty(value, limit=limit), "emoji": False}


def _field_value(field: Field) -> str:
    return "".join(span.text for span in field.value)


def _field_line(field: Field) -> str:
    value = _field_value(field)
    return f"{field.label}: {value}"


def render_slack_text(message: Message) -> str:
    """Return the plain-text Slack body for ``message``."""

    headline = _escape(message.title)
    if message.author:
        headline = f"{_escape(message.author)}: {headline}"
    lines = [headline]
    for group in message.groups:
        lines.extend(_escape(_field_line(item)) for item in group.items)
    rendered = "\n".join(lines)
    if len(rendered) > _MAX_TEXT_CHARS:
        rendered = rendered[: _MAX_TEXT_CHARS - len(_TRUNCATION_MARK)] + _TRUNCATION_MARK
    return rendered


def _sections(message: Message) -> Iterator[dict[str, Any]]:
    for group in message.groups:
        compact_fields: list[dict[str, object]] = []
        for item in group.items:
            value = _field_value(item)
            field_text = f"{item.label}\n{value}"
            if "\n" not in value and len(field_text) <= _COMPACT_FIELD_LIMIT:
                compact_fields.append(_plain_text(field_text, limit=_FIELD_TEXT_LIMIT))
                if len(compact_fields) == _MAX_FIELDS_PER_SECTION:
                    yield {"type": "section", "fields": compact_fields}
                    compact_fields = []
                continue
            if compact_fields:
                yield {"type": "section", "fields": compact_fields}
                compact_fields = []
            yield {"type": "section", "text": _plain_text(field_text, limit=_SECTION_TEXT_LIMIT)}
        if compact_fields:
            yield {"type": "section", "fields": compact_fields}


def render_slack_blocks(message: Message) -> list[dict[str, Any]]:
    """Return Block Kit blocks for ``message``."""

    blocks: list[dict[str, Any]] = []
    emoji = _SEVERITY_EMOJI.get(message.severity)
    prefix = f"{emoji} " if emoji else ""
    title_text = _non_empty(message.title, limit=max(1, _HEADER_LIMIT - len(prefix)))
    header = f"{prefix}{title_text}"
    blocks.append({"type": "header", "text": _plain_text(header, limit=_HEADER_LIMIT)})

    if message.author and message.author.strip():
        blocks.append(
            {
                "type": "context",
                "elements": [_plain_text(message.author.strip(), limit=_FIELD_TEXT_LIMIT)],
            }
        )

    remaining = _MAX_BLOCKS - len(blocks)
    sections = list(islice(_sections(message), remaining + 1))
    if len(sections) > remaining:
        sections[remaining - 1 :] = [
            {"type": "section", "text": _plain_text("More: …", limit=_SECTION_TEXT_LIMIT)}
        ]
    return blocks + sections


__all__ = ["render_slack_blocks", "render_slack_text"]
