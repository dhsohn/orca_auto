"""Display-width math for terminal tables: widths of wide and combining characters."""

from __future__ import annotations

import shutil
import unicodedata
from typing import Any


def terminal_max_width() -> int | None:
    """Return the usable terminal width, or ``None`` when it cannot be detected.

    ``shutil.get_terminal_size`` honors ``COLUMNS`` first, then queries the
    attached terminal. When output is piped (no terminal) the fallback of ``0``
    is returned here as ``None`` so piped/redirected output keeps full-width
    columns and stays stable for downstream scripts.
    """

    try:
        columns = shutil.get_terminal_size(fallback=(0, 0)).columns
    except (ValueError, OSError):
        return None
    return columns if columns > 0 else None


def table_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def char_width(char: str) -> int:
    if not char:
        return 0
    if unicodedata.combining(char):
        return 0
    if unicodedata.category(char) == "Cf":
        return 0
    if unicodedata.east_asian_width(char) in {"W", "F"}:
        return 2
    return 1


def display_width(value: str) -> int:
    return sum(char_width(char) for char in table_text(value))


def trim_to_width(value: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    trimmed: list[str] = []
    current_width = 0
    for char in table_text(value):
        char_width_value = char_width(char)
        if current_width + char_width_value > max_width:
            break
        trimmed.append(char)
        current_width += char_width_value
    return "".join(trimmed)


def truncate(value: str, *, max_width: int) -> str:
    text = table_text(value)
    if display_width(text) <= max_width:
        return text
    if max_width <= 3:
        return trim_to_width(text, max_width)
    return trim_to_width(text, max_width - 3) + "..."


def pad_right(value: str, width: int) -> str:
    padding = max(0, int(width) - display_width(value))
    return table_text(value) + (" " * padding)


__all__ = [
    "char_width",
    "display_width",
    "pad_right",
    "table_text",
    "terminal_max_width",
    "trim_to_width",
    "truncate",
]
