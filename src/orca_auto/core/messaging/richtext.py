"""Messenger-neutral rich-text document model for outbound notifications.

Domain notifiers build a :class:`Message` describing *what* to say (a title, a
severity, and a sequence of labelled fields / free-form lines). Per-messenger
renderers (:mod:`.render_telegram`, :mod:`.render_discord`) turn it into the
native markup. This keeps HTML / Markdown out of the domain code so the active
messenger can be swapped without touching any notifier.

Span construction bakes the value-vs-literal distinction in at build time so the
Telegram renderer can reproduce the pre-existing HTML byte-for-byte:

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
SpanStyle = Literal["plain", "bold", "code"]


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


@dataclass(frozen=True)
class Field:
    """A ``label: value`` row. Maps to an embed field on Discord."""

    label: str
    value: tuple[Span, ...]


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

    ``title`` is the semantic headline (Discord embed title / Telegram bold
    first line). By convention the first group's heading is ``(bold(title),)``
    so Telegram renders the title inline; the Discord renderer de-duplicates it
    against the embed title.
    """

    title: str
    severity: Severity = "info"
    groups: tuple[Group, ...] = field(default_factory=tuple)


def field_row(label: str, *value: Span) -> Field:
    """Convenience builder for a ``label: value`` field."""
    return Field(label=label, value=tuple(value))


def line(*spans: Span) -> Line:
    return Line(spans=tuple(spans))


def group(*items: Item, heading: tuple[Span, ...] = ()) -> Group:
    return Group(heading=heading, items=tuple(items))


def title_heading(title: str) -> tuple[Span, ...]:
    """The conventional first-group heading that carries the message title."""
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
    "field_row",
    "group",
    "line",
    "raw",
    "text",
    "title_heading",
]
