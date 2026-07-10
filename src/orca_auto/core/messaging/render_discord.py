"""Render a :class:`~orca_auto.core.messaging.richtext.Message` to a Discord embed.

Fields become embed fields; the title becomes the embed title (coloured by
severity); free-form lines and any non-title group headings flow into the embed
description. Discord's documented limits are enforced by truncation so a large
notification degrades gracefully instead of being rejected.
"""

from __future__ import annotations

from typing import Any

from .richtext import Field, Line, Message, Span

# Discord embed limits (https://discord.com/developers/docs/resources/channel#embed-limits)
_TITLE_LIMIT = 256
_DESCRIPTION_LIMIT = 4096
_FIELD_NAME_LIMIT = 256
_FIELD_VALUE_LIMIT = 1024
_MAX_FIELDS = 25

_SEVERITY_COLORS: dict[str, int] = {
    "info": 0x3498DB,
    "success": 0x2ECC71,
    "warning": 0xF1C40F,
    "error": 0xE74C3C,
}


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return value[: limit - 1] + "…"


def _md_span(span: Span) -> str:
    if span.style == "bold":
        return f"**{span.text}**"
    if span.style == "code":
        return f"`{span.text}`"
    return span.text


def _md_spans(spans: tuple[Span, ...]) -> str:
    return "".join(_md_span(span) for span in spans)


def _is_title_heading(heading: tuple[Span, ...], title: str) -> bool:
    return len(heading) == 1 and heading[0].style == "bold" and heading[0].text == title


def _collect(
    message: Message,
) -> tuple[list[str], list[dict[str, Any]]]:
    description_lines: list[str] = []
    fields: list[dict[str, Any]] = []
    for index, group in enumerate(message.groups):
        if group.heading and not (index == 0 and _is_title_heading(group.heading, message.title)):
            description_lines.append(_md_spans(group.heading))
        for item in group.items:
            if isinstance(item, Field):
                fields.append(
                    {
                        "name": _truncate(item.label, _FIELD_NAME_LIMIT) or "-",
                        "value": _truncate(_md_spans(item.value), _FIELD_VALUE_LIMIT) or "-",
                        "inline": False,
                    }
                )
            elif isinstance(item, Line):
                description_lines.append(_md_spans(item.spans))
    return description_lines, fields


def render_discord_embed(message: Message) -> dict[str, Any]:
    """Return a single Discord embed object for ``message``."""
    description_lines, fields = _collect(message)
    embed: dict[str, Any] = {"title": _truncate(message.title, _TITLE_LIMIT)}
    color = _SEVERITY_COLORS.get(message.severity)
    if color is not None:
        embed["color"] = color
    if description_lines:
        embed["description"] = _truncate("\n".join(description_lines), _DESCRIPTION_LIMIT)
    if fields:
        embed["fields"] = fields[:_MAX_FIELDS]
    return embed


__all__ = ["render_discord_embed"]
