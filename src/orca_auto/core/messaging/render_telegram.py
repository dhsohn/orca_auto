"""Render a :class:`~orca_auto.core.messaging.richtext.Message` to Telegram HTML.

The output is intentionally byte-identical to the hand-written HTML the notifiers
produced before the Doc model was introduced: value spans were normalised with
``str(value).strip()`` at build time, so here we only HTML-escape (no stripping)
and wrap with ``<b>`` / ``<code>`` exactly as ``escape_html`` / ``html_code`` did.
"""

from __future__ import annotations

from .richtext import Field, Group, Line, Message, Span


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_span(span: Span) -> str:
    escaped = _escape(span.text)
    if span.style == "bold":
        return f"<b>{escaped}</b>"
    if span.style == "code":
        return f"<code>{escaped}</code>"
    return escaped


def _render_spans(spans: tuple[Span, ...]) -> str:
    return "".join(_render_span(span) for span in spans)


def _render_item(item: Field | Line) -> str:
    if isinstance(item, Field):
        return f"<b>{_escape(item.label)}</b>: {_render_spans(item.value)}"
    return _render_spans(item.spans)


def _render_group(group: Group) -> str:
    lines: list[str] = []
    if group.heading:
        lines.append(_render_spans(group.heading))
    lines.extend(_render_item(item) for item in group.items)
    return "\n".join(lines)


def render_telegram(message: Message) -> str:
    """Return the Telegram HTML body for ``message``."""
    return "\n\n".join(_render_group(group) for group in message.groups)


__all__ = ["render_telegram"]
