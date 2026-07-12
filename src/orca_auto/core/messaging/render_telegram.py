"""Render a :class:`~orca_auto.core.messaging.richtext.Message` to Telegram HTML.

The output is intentionally byte-identical to the hand-written HTML the notifiers
produced before the Doc model was introduced: value spans were normalised with
``str(value).strip()`` at build time, so here we only HTML-escape (no stripping)
and wrap with ``<b>`` / ``<code>`` exactly as ``escape_html`` / ``html_code`` did.
"""

from __future__ import annotations

from dataclasses import dataclass

from .richtext import Field, Line, Message, Span, SpanStyle

# Telegram Bot API sendMessage text limit after entity parsing.
_TELEGRAM_MESSAGE_LIMIT = 4096


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_span(span: Span) -> str:
    escaped = _escape(span.text)
    if span.style == "bold":
        return f"<b>{escaped}</b>"
    if span.style == "code":
        return f"<code>{escaped}</code>"
    return escaped


def _span_text(spans: tuple[Span, ...]) -> str:
    return "".join(span.text for span in spans)


def _contains_inline_title(spans: tuple[Span, ...], title: str) -> bool:
    return _span_text(spans) == title or any(
        span.style == "bold" and span.text == title for span in spans
    )


def _has_inline_title(message: Message) -> bool:
    if not message.groups:
        return False
    first_group = message.groups[0]
    if first_group.heading and _contains_inline_title(first_group.heading, message.title):
        return True
    if first_group.items and isinstance(first_group.items[0], Line):
        return _contains_inline_title(first_group.items[0].spans, message.title)
    return False


def _message_spans(message: Message) -> tuple[Span, ...]:
    """Flatten a message while retaining span boundaries.

    The title belongs to :class:`Message`, not to a caller-side rendering
    convention.  Existing builders still put the title in the first group;
    the equality check avoids duplicating it while making title-only messages
    work on Telegram too.
    """

    tokens: list[Span] = []
    if message.author and message.author.strip():
        # Discord shows the identity as the embed author line; Telegram has no
        # such slot, so surface it as a leading plain line above the bold title.
        # Strip to match the Discord renderer (a blank author is omitted, not a
        # blank line).
        tokens.append(Span(message.author.strip(), "plain"))
        if message.title or message.groups:
            tokens.append(Span("\n", "plain"))
    if message.title and not _has_inline_title(message):
        tokens.append(Span(message.title, "bold"))
        if message.groups:
            tokens.append(Span("\n", "plain"))

    for group_index, current_group in enumerate(message.groups):
        if group_index:
            tokens.append(Span("\n\n", "plain"))

        lines: list[tuple[Span, ...]] = []
        if current_group.heading:
            lines.append(current_group.heading)
        for item in current_group.items:
            if isinstance(item, Field):
                lines.append(
                    (
                        Span(item.label, "bold"),
                        Span(": ", "plain"),
                        *item.value,
                    )
                )
            else:
                lines.append(item.spans)

        for line_index, current_line in enumerate(lines):
            if line_index:
                tokens.append(Span("\n", "plain"))
            tokens.extend(current_line)

    return tuple(tokens)


@dataclass(frozen=True)
class TelegramRenderedChunk:
    """One valid HTML chunk and its independently rendered plain fallback."""

    html: str
    plain: str


def _largest_prefix_that_fits(text: str, style: SpanStyle, available: int) -> int:
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        rendered = _render_span(Span(text[:middle], style))
        if len(rendered) <= available:
            low = middle
        else:
            high = middle - 1
    return low


def render_telegram_chunks(
    message: Message,
    *,
    limit: int = _TELEGRAM_MESSAGE_LIMIT,
) -> tuple[TelegramRenderedChunk, ...]:
    """Render Telegram chunks without ever splitting an HTML entity or tag.

    Telegram applies the message-size limit after entity parsing.  Bounding the
    encoded HTML as well is intentionally conservative and makes every emitted
    request independently valid.  The plain fallback is built from the source
    spans, so a parse error never exposes HTML tags to the recipient.
    """

    if limit <= len("<code></code>"):
        raise ValueError("Telegram chunk limit is too small for styled text")

    chunks: list[TelegramRenderedChunk] = []
    current_html = ""
    current_plain = ""

    def flush() -> None:
        nonlocal current_html, current_plain
        if current_html:
            chunks.append(TelegramRenderedChunk(current_html, current_plain))
        current_html = ""
        current_plain = ""

    for token in _message_spans(message):
        remaining = token.text
        while remaining:
            available = limit - len(current_html)
            prefix_length = _largest_prefix_that_fits(remaining, token.style, available)
            if prefix_length == 0:
                if current_html:
                    flush()
                    continue
                raise ValueError(
                    "Telegram chunk limit cannot fit one escaped character with its style"
                )

            prefix = remaining[:prefix_length]
            current_html += _render_span(Span(prefix, token.style))
            current_plain += prefix
            remaining = remaining[prefix_length:]
            if remaining:
                flush()

    flush()
    return tuple(chunks)


def render_telegram(message: Message) -> str:
    """Return the Telegram HTML body for ``message``."""
    return "".join(_render_span(span) for span in _message_spans(message))


__all__ = ["TelegramRenderedChunk", "render_telegram", "render_telegram_chunks"]
