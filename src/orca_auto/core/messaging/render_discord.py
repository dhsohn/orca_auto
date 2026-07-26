"""Render a :class:`~orca_auto.core.messaging.richtext.Message` to a Discord embed.

Fields become embed fields; the title becomes the embed title (coloured by
severity); free-form lines and any non-title group headings flow into the embed
description. Discord's documented limits are enforced by truncation so a large
notification degrades gracefully instead of being rejected.
"""

from __future__ import annotations

import re
from typing import Any

from .richtext import Field, Line, Message, Span

# Discord embed limits (https://discord.com/developers/docs/resources/channel#embed-limits)
_TITLE_LIMIT = 256
_DESCRIPTION_LIMIT = 4096
_FIELD_NAME_LIMIT = 256
_FIELD_VALUE_LIMIT = 1024
_MAX_FIELDS = 25
_EMBED_TOTAL_LIMIT = 6000
_OMITTED_FIELD_NAME = "More"
_OMITTED_FIELD_VALUE = "…"

_MARKDOWN_SPECIAL = re.compile(r"([\\\\`*_{}\[\]<>()#+\-.!|>~])")

_SEVERITY_COLORS: dict[str, int] = {
    "info": 0x3498DB,
    "success": 0x2ECC71,
    "warning": 0xF1C40F,
    "error": 0xE74C3C,
}

# A leading glyph makes the outcome scannable at a glance. ``info`` is left
# bare on purpose so routine, non-outcome events stay visually quiet.
_SEVERITY_EMOJI: dict[str, str] = {
    "success": "✅",
    "warning": "⚠️",
    "error": "❌",
}

_AUTHOR_NAME_LIMIT = 256


def _escape_markdown(value: str) -> str:
    """Escape formatting delimiters in provider-neutral plain text."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", value)


def _truncate(value: str, limit: int) -> str:
    """Length-bound plain text without Markdown escaping.

    Discord renders the embed title, author name, and field names as literal
    text — none parse Markdown — so escaping them would surface visible
    backslashes. Only the description and field values (which do parse Markdown)
    are escaped.
    """
    if len(value) <= limit:
        return value
    if limit <= 1:
        return "…"[:limit]
    return value[: limit - 1] + "…"


def _code_span(value: str) -> str:
    if not value:
        return ""
    longest_run = max((len(run) for run in re.findall(r"`+", value)), default=0)
    fence = "`" * (longest_run + 1)
    # Padding is required when the content itself starts/ends with a backtick;
    # it also keeps a multi-backtick delimiter unambiguous to Discord.
    padding = " " if longest_run else ""
    return f"{fence}{padding}{value}{padding}{fence}"


def _md_span(span: Span) -> str:
    if span.style == "bold":
        return f"**{_escape_markdown(span.text)}**" if span.text else ""
    if span.style == "code":
        return _code_span(span.text)
    return _escape_markdown(span.text)


def _md_spans(spans: tuple[Span, ...], *, limit: int | None = None) -> str:
    rendered = [_md_span(span) for span in spans]
    joined = "".join(rendered)
    if limit is None or len(joined) <= limit:
        return joined
    if limit <= 1:
        return "…"[:limit]

    # Reserve one character for an explicit continuation marker. Truncate at
    # Span boundaries (or within one Span) so bold/code delimiters stay closed.
    budget = limit - 1
    output: list[str] = []
    for span, value in zip(spans, rendered, strict=True):
        if len(value) <= budget:
            output.append(value)
            budget -= len(value)
            continue
        low, high = 0, len(span.text)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = _md_span(Span(span.text[:middle], span.style))
            if len(candidate) <= budget:
                low = middle
            else:
                high = middle - 1
        if low:
            output.append(_md_span(Span(span.text[:low], span.style)))
        output.append("…")
        return "".join(output)
    output.append("…")
    return "".join(output)


def _collect(
    message: Message,
) -> tuple[list[tuple[Span, ...]], list[Field]]:
    description_lines: list[tuple[Span, ...]] = []
    fields: list[Field] = []
    for group in message.groups:
        if group.heading:
            description_lines.append(group.heading)
        for item in group.items:
            if isinstance(item, Field):
                fields.append(item)
            elif isinstance(item, Line):
                description_lines.append(item.spans)
    return description_lines, fields


def _render_description(lines: list[tuple[Span, ...]], limit: int) -> str:
    rendered = [_md_spans(spans) for spans in lines]
    joined = "\n".join(rendered)
    if len(joined) <= limit:
        return joined
    if limit <= 1:
        return "…"[:limit]

    output: list[str] = []
    used = 0
    for index, (spans, value) in enumerate(zip(lines, rendered, strict=True)):
        separator = 1 if output else 0
        remaining = limit - used - separator
        if remaining <= 0:
            # Earlier iterations always reserve room when another line follows.
            # This is defensive for a zero-width/empty-line edge case.
            return "\n".join(output)
        more_after = index + 1 < len(lines)
        if more_after and len(value) + 2 > remaining:
            # Add a sentinel Span so the bounded renderer knows there is more
            # logical content and closes any active Markdown before adding `…`.
            continued = spans + (Span("\n"),)
            output.append(_md_spans(continued, limit=remaining))
            return "\n".join(output)
        if len(value) <= remaining:
            output.append(value)
            used += separator + len(value)
            continue
        output.append(_md_spans(spans, limit=remaining))
        return "\n".join(output)
    return "\n".join(output)


def _field_payload(field: Field, *, budget: int) -> tuple[dict[str, Any] | None, bool]:
    """Render one field inside ``budget`` and report whether it was truncated."""
    if budget < 2:
        return None, True
    full_name = _truncate(field.label, _FIELD_NAME_LIMIT) or "-"
    full_value = _md_spans(field.value, limit=_FIELD_VALUE_LIMIT) or "-"
    if len(full_name) + len(full_value) <= budget:
        return {"name": full_name, "value": full_value, "inline": field.inline}, False

    name_limit = min(_FIELD_NAME_LIMIT, max(1, budget - 1))
    name = _truncate(field.label, name_limit) or "-"
    value_limit = min(_FIELD_VALUE_LIMIT, max(1, budget - len(name)))
    value = _md_spans(field.value, limit=value_limit) or "-"
    return {"name": name, "value": value, "inline": field.inline}, True


def render_discord_embed(message: Message) -> dict[str, Any]:
    """Return a single Discord embed object for ``message``."""
    description_lines, fields = _collect(message)
    emoji = _SEVERITY_EMOJI.get(message.severity)
    prefix = f"{emoji} " if emoji else ""
    title_text = _truncate(message.title, _TITLE_LIMIT - len(prefix)) or "-"
    title = f"{prefix}{title_text}"
    embed: dict[str, Any] = {"title": title}
    remaining = _EMBED_TOTAL_LIMIT - len(title)
    color = _SEVERITY_COLORS.get(message.severity)
    if color is not None:
        embed["color"] = color
    if message.author and message.author.strip():
        author_name = _truncate(message.author.strip(), min(_AUTHOR_NAME_LIMIT, max(1, remaining)))
        if author_name:
            embed["author"] = {"name": author_name}
            remaining -= len(author_name)
    if description_lines:
        description = _render_description(
            description_lines,
            min(_DESCRIPTION_LIMIT, remaining),
        )
        if description:
            embed["description"] = description
            remaining -= len(description)
    if fields:
        rendered_fields: list[dict[str, Any]] = []
        omitted = len(fields) > _MAX_FIELDS
        regular_limit = _MAX_FIELDS - 1 if omitted else _MAX_FIELDS
        marker_cost = len(_OMITTED_FIELD_NAME) + len(_OMITTED_FIELD_VALUE)
        for index, item in enumerate(fields[:regular_limit]):
            more_after = index + 1 < len(fields)
            reserve = marker_cost if more_after else 0
            payload, truncated = _field_payload(item, budget=max(0, remaining - reserve))
            if payload is None:
                omitted = True
                break
            rendered_fields.append(payload)
            consumed = len(str(payload["name"])) + len(str(payload["value"]))
            remaining -= consumed
            if truncated:
                omitted = True
                break
        if len(rendered_fields) < min(len(fields), regular_limit):
            omitted = True
        if omitted and len(rendered_fields) < _MAX_FIELDS and remaining >= marker_cost:
            rendered_fields.append(
                {
                    "name": _OMITTED_FIELD_NAME,
                    "value": _OMITTED_FIELD_VALUE,
                    "inline": False,
                }
            )
        if rendered_fields:
            embed["fields"] = rendered_fields
    return embed


__all__ = ["render_discord_embed"]
