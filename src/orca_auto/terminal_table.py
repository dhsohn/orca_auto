from __future__ import annotations

import shutil
import unicodedata
from collections.abc import Callable, Sequence
from typing import Any

# Gap rendered between adjacent table columns.
QUEUE_COLUMN_GAP = "  "

# Smallest width each flexible column may shrink to under terminal-width
# pressure, and the order in which columns surrender space (least essential
# first). ``id`` shrinks last because it doubles as the ``queue cancel`` target.
QUEUE_MIN_WIDTHS = {"detail": 6, "name": 8, "id": 8}
QUEUE_SHRINK_ORDER = ("detail", "name", "id")
QUEUE_HEADERS = {
    "status": "Status",
    "name": "Name",
    "detail": "Detail",
    "id": "ID",
    "elapsed": "Elapsed",
}


def terminal_max_width(
    *,
    get_terminal_size: Callable[..., Any] | None = None,
) -> int | None:
    """Return the usable terminal width, or ``None`` when it cannot be detected.

    ``shutil.get_terminal_size`` honors ``COLUMNS`` first, then queries the
    attached terminal. When output is piped (no terminal) the fallback of ``0``
    is returned here as ``None`` so piped/redirected output keeps full-width
    columns and stays stable for downstream scripts.
    """

    try:
        getter = get_terminal_size or shutil.get_terminal_size
        columns = getter(fallback=(0, 0)).columns
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


def fit_queue_widths(widths: dict[str, int], *, max_total: int | None) -> dict[str, int]:
    """Shrink flexible columns so the rendered row fits ``max_total`` columns.

    Columns are reduced in ``QUEUE_SHRINK_ORDER`` down to ``QUEUE_MIN_WIDTHS``;
    if the row still overflows after every column hits its floor the widths are
    left at their minimums (the row may wrap, but never silently misaligns).
    """

    if max_total is None:
        return widths
    gaps = QUEUE_COLUMN_GAP * (len(widths) - 1)
    overflow = sum(widths.values()) + display_width(gaps) - max_total
    if overflow <= 0:
        return widths
    adjusted = dict(widths)
    for key in QUEUE_SHRINK_ORDER:
        if overflow <= 0:
            break
        reducible = adjusted[key] - QUEUE_MIN_WIDTHS[key]
        if reducible <= 0:
            continue
        width_reduction = min(reducible, overflow)
        adjusted[key] -= width_reduction
        overflow -= width_reduction
    return adjusted


def queue_table_columns(*, include_id: bool) -> list[str]:
    # ``id`` is dropped for narrow surfaces where a
    # full activity id would wrap each row onto a second line.
    return ["status", "name", "detail"] + (["id"] if include_id else []) + ["elapsed"]


def queue_table_header_width(key: str, prepared: Sequence[dict[str, str]]) -> int:
    return max(
        display_width(QUEUE_HEADERS[key]),
        max((display_width(row[key]) for row in prepared), default=0),
    )


def queue_table_widths(
    prepared: Sequence[dict[str, str]],
    columns: Sequence[str],
    *,
    max_width: int | None = None,
) -> dict[str, int]:
    # Soft caps keep wide values from dominating before terminal-fit shrinking.
    widths = {key: queue_table_header_width(key, prepared) for key in columns}
    widths["detail"] = max(display_width(QUEUE_HEADERS["detail"]), min(36, widths["detail"]))
    widths["name"] = max(display_width(QUEUE_HEADERS["name"]), min(32, widths["name"]))
    widths["elapsed"] = max(display_width(QUEUE_HEADERS["elapsed"]), 8)

    # ``status`` and ``elapsed`` are intrinsically narrow and fixed, so the
    # flexible text columns absorb any terminal-width shortfall.
    gap_width = display_width(QUEUE_COLUMN_GAP) * (len(columns) - 1)
    fixed_width = widths["status"] + widths["elapsed"] + gap_width
    flexible_keys = [key for key in QUEUE_SHRINK_ORDER if key in widths]
    flexible = fit_queue_widths(
        {key: widths[key] for key in flexible_keys},
        max_total=None if max_width is None else max(0, max_width - fixed_width),
    )
    widths.update(flexible)
    return widths


def render_queue_table_row(
    values: dict[str, str],
    *,
    columns: Sequence[str],
    widths: dict[str, int],
) -> str:
    return QUEUE_COLUMN_GAP.join(
        pad_right(truncate(values[key], max_width=widths[key]), widths[key]) for key in columns
    )


def queue_table_lines(
    prepared: Sequence[dict[str, str]],
    *,
    max_width: int | None = None,
    include_id: bool = True,
) -> list[str]:
    columns = queue_table_columns(include_id=include_id)
    widths = queue_table_widths(prepared, columns, max_width=max_width)
    gap_width = display_width(QUEUE_COLUMN_GAP) * (len(columns) - 1)

    def render_row(values: dict[str, str]) -> str:
        return render_queue_table_row(values, columns=columns, widths=widths)

    lines = [
        render_row(QUEUE_HEADERS),
        "─" * (sum(widths[key] for key in columns) + gap_width),
    ]
    lines.extend(render_row(row) for row in prepared)
    return lines


__all__ = [
    "QUEUE_COLUMN_GAP",
    "QUEUE_HEADERS",
    "QUEUE_MIN_WIDTHS",
    "QUEUE_SHRINK_ORDER",
    "char_width",
    "display_width",
    "fit_queue_widths",
    "pad_right",
    "queue_table_columns",
    "queue_table_header_width",
    "queue_table_lines",
    "queue_table_widths",
    "render_queue_table_row",
    "table_text",
    "terminal_max_width",
    "trim_to_width",
    "truncate",
]
