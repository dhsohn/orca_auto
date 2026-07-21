"""Small HTML-escaping helpers for report rendering."""

from __future__ import annotations

from typing import Any

from orca_auto.core.utils.coercion import normalize_text as _normalize_text


def escape_html_text(text: str) -> str:
    """Escape the HTML metacharacters without normalizing whitespace."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_html(value: Any) -> str:
    return escape_html_text(_normalize_text(value))


def html_code(value: Any) -> str:
    return f"<code>{escape_html(value)}</code>"


__all__ = ["escape_html", "escape_html_text", "html_code"]
