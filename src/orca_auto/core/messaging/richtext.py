"""Messenger-neutral rich-text document model for outbound notifications.

Domain notifiers build a :class:`Message` describing *what* to say (a title, a
severity, and a sequence of labelled fields / free-form lines). Per-messenger
renderers (:mod:`.render_discord`) turn it into the native markup. This keeps
HTML / Markdown out of the domain code so the active messenger can be swapped
without touching any notifier.

Span construction bakes the value-vs-literal distinction in at build time so each
renderer can preserve the intended text semantics:

* :func:`text`, :func:`bold`, :func:`code` normalise their value with
  ``str(value).strip()`` — matching the old ``escape_html`` / ``html_code``.
* :func:`raw` keeps the string verbatim (significant leading whitespace, e.g.
  indented monitor rows) and is only HTML-escaped, never stripped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from orca_auto.core.utils.coercion import normalize_text

Severity = Literal["info", "success", "warning", "error"]
SpanStyle = Literal["plain", "bold", "code", "pre"]


@dataclass(frozen=True)
class Span:
    """A run of inline text with a single style."""

    text: str
    style: SpanStyle = "plain"


def text(value: object) -> Span:
    """Plain value span (stripped, escaped at render time)."""
    return Span(normalize_text(value), "plain")


def raw(value: object) -> Span:
    """Literal plain span kept verbatim — preserves significant whitespace."""
    return Span(str(value), "plain")


def bold(value: object) -> Span:
    return Span(normalize_text(value), "bold")


def code(value: object) -> Span:
    return Span(normalize_text(value), "code")


def code_block(value: object) -> Span:
    """A preformatted block, kept verbatim (newlines/whitespace preserved).

    Renders as a fenced code block on Discord.
    Use it for tables and other monospace content; put it in its own
    :func:`line` so it stands alone.
    """
    return Span(str(value), "pre")


@dataclass(frozen=True)
class Field:
    """A ``label: value`` row. Maps to an embed field on Discord.

    ``inline`` is a Discord layout hint: inline fields sit side by side (up to
    three per row) instead of each spanning the full width.
    """

    label: str
    value: tuple[Span, ...]
    inline: bool = False


@dataclass(frozen=True)
class Line:
    """A free-form line of spans (dividers, cards, notes)."""

    spans: tuple[Span, ...]


Item = Field | Line


@dataclass(frozen=True)
class Group:
    """A paragraph: an optional heading line followed by fields / lines.

    Renderers join groups with a blank line and items within a group with a
    single newline.
    """

    heading: tuple[Span, ...] = ()
    items: tuple[Item, ...] = ()


@dataclass(frozen=True)
class Message:
    """A complete notification.

    ``title`` is the semantic headline (Discord embed title). Renderers own its
    native representation. Existing builders may
    still include the same bold title in a decorated first line; renderers
    detect that form and avoid duplicating it.

    ``author`` is an optional sender identity shown above the title: the embed
    author line on Discord. It lets a builder drop a redundant "orca_auto …"
    prefix from the title and surface the identity as chrome instead.
    """

    title: str
    severity: Severity = "info"
    groups: tuple[Group, ...] = field(default_factory=tuple)
    author: str | None = None


def field_row(label: str, *value: Span, inline: bool = False) -> Field:
    """Convenience builder for a ``label: value`` field."""
    return Field(label=label, value=tuple(value), inline=inline)


def line(*spans: Span) -> Line:
    return Line(spans=tuple(spans))


def group(*items: Item, heading: tuple[Span, ...] = ()) -> Group:
    return Group(heading=heading, items=tuple(items))


def title_heading(title: str) -> tuple[Span, ...]:
    """Build an explicit first-group title heading for legacy-stable layouts."""
    return (bold(title),)


__all__ = [
    "Field",
    "Group",
    "Item",
    "Line",
    "Message",
    "Severity",
    "Span",
    "SpanStyle",
    "bold",
    "code",
    "code_block",
    "field_row",
    "group",
    "line",
    "raw",
    "text",
    "title_heading",
]
