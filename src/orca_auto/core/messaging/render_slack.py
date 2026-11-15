"""Render a :class:`~orca_auto.core.messaging.richtext.Message` for Slack.

Top-level ``text`` is plain with Markdown off; ``&``, ``<`` and ``>`` are escaped
so nothing in a message can mention or link. Top-level ``blocks`` are native
``rich_text`` blocks whose leaves are typed ``text`` elements only (never
``mrkdwn`` or link/mention element types).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .richtext import Field, Message, Span

_MAX_TEXT_CHARS = 3000
_MAX_VISIBLE_CHARS = 3000
_MAX_TEXT_LEAVES = 200
_HEADER_LIMIT = 150
_TRUNCATION_MARK = "…"

_SEVERITY_GLYPHS: dict[str, str] = {
    "info": "ℹ️",
    "success": "✅",
    "warning": "⚠️",
    "error": "❌",
}


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    mark_len = len(_TRUNCATION_MARK)
    if limit <= mark_len:
        return _TRUNCATION_MARK[:limit]
    return value[: limit - mark_len] + _TRUNCATION_MARK


def _non_empty(value: str, *, limit: int) -> str:
    trimmed = _truncate(value.strip(), limit)
    return trimmed or "-"


def _text_leaf(text: str, *, bold: bool = False, code: bool = False) -> dict[str, object]:
    leaf: dict[str, object] = {"type": "text", "text": text}
    style: dict[str, bool] = {}
    if bold:
        style["bold"] = True
    if code:
        style["code"] = True
    if style:
        leaf["style"] = style
    return leaf


def _rich_text_block(*sections: dict[str, object]) -> dict[str, object]:
    return {"type": "rich_text", "elements": list(sections)}


def _rich_text_section(*elements: dict[str, object]) -> dict[str, object]:
    return {"type": "rich_text_section", "elements": list(elements)}


def _field_value(field: Field) -> str:
    return "".join(span.text for span in field.value)


def _field_line(field: Field) -> str:
    value = _field_value(field)
    return f"{field.label}: {value}"


@dataclass
class _VisibleBudget:
    chars_remaining: int = _MAX_VISIBLE_CHARS
    leaves_remaining: int = _MAX_TEXT_LEAVES
    halted: bool = False

    def mark_halt(self) -> None:
        self.halted = True


def _append_leaf(
    elements: list[dict[str, object]],
    budget: _VisibleBudget,
    text: str,
    *,
    bold: bool = False,
    code: bool = False,
    more_after: bool = False,
) -> bool:
    """Append one typed ``text`` leaf when budget allows. Return False when exhausted."""

    if not text:
        return True
    if budget.halted or budget.leaves_remaining <= 0 or budget.chars_remaining <= 0:
        budget.mark_halt()
        return False

    limit = budget.chars_remaining
    at_last_leaf = budget.leaves_remaining == 1
    mark_len = len(_TRUNCATION_MARK)
    omitted_here = False
    piece: str

    if len(text) > limit:
        piece = _truncate(text, limit)
        omitted_here = True
    elif more_after and len(text) == limit:
        piece = text[: limit - mark_len] + _TRUNCATION_MARK
        omitted_here = True
    elif more_after and at_last_leaf:
        if len(text) + mark_len <= limit:
            piece = f"{text}{_TRUNCATION_MARK}"
        else:
            piece = _truncate(text, limit)
        omitted_here = True
    else:
        piece = text

    elements.append(_text_leaf(piece, bold=bold, code=code))
    budget.chars_remaining -= len(piece)
    budget.leaves_remaining -= 1
    if omitted_here:
        budget.mark_halt()
    return True


def _has_nonempty_fields(message: Message) -> bool:
    return any(group.items for group in message.groups)


def _emit_span_leaves(
    elements: list[dict[str, object]],
    budget: _VisibleBudget,
    spans: tuple[Span, ...],
    *,
    more_after: bool,
) -> bool:
    """Emit span leaves; return False when visible budget is exhausted."""

    nonempty = (span for span in spans if span.text)
    current = next(nonempty, None)
    if current is None:
        return _append_leaf(elements, budget, "-", more_after=more_after)

    while current is not None:
        nxt = next(nonempty, None)
        span_more = nxt is not None or more_after
        if not _append_leaf(
            elements,
            budget,
            current.text,
            code=current.style == "code",
            more_after=span_more,
        ):
            return False
        current = nxt
    return True


def _emit_field(
    elements: list[dict[str, object]],
    budget: _VisibleBudget,
    field: Field,
    *,
    more_after: bool,
) -> bool:
    label = f"{field.label}: "
    if not _append_leaf(elements, budget, label, bold=True, more_after=True):
        return False
    return _emit_span_leaves(elements, budget, field.value, more_after=more_after)


def _field_rows(message: Message) -> Iterator[tuple[bool, Field | None]]:
    """Yield ``(is_spacer, field)``; spacers mark blank lines between groups."""

    first = True
    for group in message.groups:
        if not group.items:
            continue
        if not first:
            yield True, None
        first = False
        for item in group.items:
            yield False, item


def _body_leaves(message: Message, budget: _VisibleBudget) -> list[dict[str, object]]:
    elements: list[dict[str, object]] = []
    row_iter = iter(_field_rows(message))
    try:
        current = next(row_iter)
    except StopIteration:
        return elements

    first_field_in_body = True
    while True:
        try:
            nxt = next(row_iter)
            has_following = True
        except StopIteration:
            nxt = None
            has_following = False

        is_spacer, field = current
        if is_spacer:
            if not _append_leaf(elements, budget, "\n\n", more_after=has_following):
                break
            first_field_in_body = True
        else:
            assert field is not None
            if not first_field_in_body:
                if not _append_leaf(elements, budget, "\n", more_after=True):
                    break
            if not _emit_field(elements, budget, field, more_after=has_following):
                break
            first_field_in_body = False

        if nxt is None:
            break
        current = nxt

    return elements


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


def render_slack_blocks(message: Message) -> list[dict[str, Any]]:
    """Return top-level ``rich_text`` Block Kit blocks for ``message``."""

    budget = _VisibleBudget()
    title_elements: list[dict[str, object]] = []

    glyph = _SEVERITY_GLYPHS.get(message.severity, _SEVERITY_GLYPHS["info"])
    prefix = f"{glyph} "
    title_limit = max(1, _HEADER_LIMIT - len(prefix))
    title_text = _non_empty(message.title, limit=title_limit)
    has_body = _has_nonempty_fields(message)

    _append_leaf(title_elements, budget, prefix, more_after=True)
    _append_leaf(
        title_elements,
        budget,
        title_text,
        bold=True,
        more_after=bool(message.author and message.author.strip()) or has_body,
    )
    if message.author and message.author.strip():
        author_suffix = f" · {message.author.strip()}"
        _append_leaf(title_elements, budget, author_suffix, more_after=has_body)

    body_elements: list[dict[str, object]] = []
    if has_body and not budget.halted:
        if _append_leaf(title_elements, budget, "\n", more_after=True):
            body_elements = _body_leaves(message, budget)

    elements = [*title_elements, *body_elements]
    block = _rich_text_block(_rich_text_section(*elements))
    return [block]


__all__ = [
    "render_slack_blocks",
    "render_slack_text",
]
