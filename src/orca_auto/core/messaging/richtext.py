"""Messenger-neutral document model for outbound notifications.

Domain notifiers build a :class:`Message` describing *what* to say (a title, a
severity, an optional author, and groups of labelled fields). Per-messenger
renderers (:mod:`.render_discord`) turn it into the native markup. This keeps
Markdown out of the domain code.

Span construction bakes the value-vs-literal distinction in at build time so the
renderer can preserve the intended text semantics:

* :func:`text`, :func:`code` normalise their value with
  ``str(value).strip()``.
* :func:`raw` keeps the string verbatim (significant leading whitespace) and is
  only Markdown-escaped, never stripped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from orca_auto.core.utils.coercion import normalize_text

Severity = Literal["info", "success", "warning", "error"]
SpanStyle = Literal["plain", "code"]


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


def code(value: object) -> Span:
    return Span(normalize_text(value), "code")


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
class Group:
    """A run of fields rendered together."""

    items: tuple[Field, ...] = ()


@dataclass(frozen=True)
class Message:
    """A complete notification.

    ``title`` is the semantic headline (Discord embed title); ``author`` is an
    optional sender identity shown above it (the embed author line), so builders
    can surface "orca_auto" as chrome instead of a title prefix.
    """

    title: str
    severity: Severity = "info"
    groups: tuple[Group, ...] = field(default_factory=tuple)
    author: str | None = None


def field_row(label: str, *value: Span, inline: bool = False) -> Field:
    """Convenience builder for a ``label: value`` field."""
    return Field(label=label, value=tuple(value), inline=inline)


def group(*items: Field) -> Group:
    return Group(items=tuple(items))


__all__ = [
    "Field",
    "Group",
    "Message",
    "Severity",
    "Span",
    "SpanStyle",
    "code",
    "field_row",
    "group",
    "raw",
    "text",
]
