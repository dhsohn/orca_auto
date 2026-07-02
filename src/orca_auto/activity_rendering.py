from __future__ import annotations

import shutil
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from orca_auto import activity_labels as _activity_labels
from orca_auto import terminal_table as _terminal_table
from orca_auto.core.utils import normalize_text


def _queue_table_now() -> datetime:
    return _activity_labels.queue_table_now()


def _queue_elapsed_text(item: dict[str, Any], *, now: datetime | None = None) -> str:
    return _activity_labels.queue_elapsed_text(item, now=now, now_factory=_queue_table_now)


def _queue_status_icon(item: dict[str, Any]) -> str:
    return _activity_labels.queue_status_icon(item)


def _queue_detail_text(item: dict[str, Any]) -> str:
    return _activity_labels.queue_detail_text(item)


def _queue_name_text(item: dict[str, Any]) -> str:
    return _activity_labels.queue_name_text(item)


def _queue_display_width(value: str) -> int:
    return _terminal_table.display_width(value)


def _terminal_max_width() -> int | None:
    return _terminal_table.terminal_max_width(get_terminal_size=shutil.get_terminal_size)


def _prepare_queue_table_rows(
    rows: Sequence[tuple[int, dict[str, Any]]],
    *,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    prepared: list[dict[str, str]] = []
    resolved_now = now or _queue_table_now()
    for indent, item in rows:
        name = _queue_name_text(item)
        if int(indent) > 0:
            name = ("  " * int(indent)) + name
        item_id = normalize_text(item.get("activity_id")) or "-"
        prepared.append(
            {
                "status": _queue_status_icon(item),
                "name": name,
                "detail": _queue_detail_text(item),
                "id": item_id,
                "elapsed": _queue_elapsed_text(item, now=resolved_now),
            }
        )
    return prepared


def queue_table_lines(
    rows: Sequence[tuple[int, dict[str, Any]]],
    *,
    now: datetime | None = None,
    max_width: int | None = None,
    include_id: bool = True,
) -> list[str]:
    prepared = _prepare_queue_table_rows(rows, now=now)
    return _terminal_table.queue_table_lines(
        prepared,
        max_width=max_width,
        include_id=include_id,
    )


def queue_list_text_lines(
    rows: Sequence[tuple[int, dict[str, Any]]],
    *,
    active_simulations: int,
    now: datetime | None = None,
    max_width: int | None = None,
    include_id: bool = True,
    empty_message: str = "No matching activities.",
) -> list[str]:
    lines = [f"active_simulations: {int(active_simulations)}"]
    if not rows:
        lines.append(empty_message)
        return lines
    lines.extend(queue_table_lines(rows, now=now, max_width=max_width, include_id=include_id))
    return lines


def queue_clear_lines(payload: dict[str, Any]) -> list[str]:
    total_cleared = int(payload.get("total_cleared", 0) or 0)
    if total_cleared <= 0:
        return ["Nothing to clear."]

    lines = [f"Cleared {total_cleared} completed/failed/cancelled entries."]
    cleared = payload.get("cleared")
    if not isinstance(cleared, dict):
        return lines

    labels = (
        ("workflows", "workflows"),
        ("xtb_queue_entries", "xTB queue entries"),
        ("crest_queue_entries", "CREST queue entries"),
        ("orca_queue_entries", "ORCA queue entries"),
        ("orca_run_states", "ORCA run states"),
    )
    for key, label in labels:
        count = int(cleared.get(key, 0) or 0)
        if count > 0:
            lines.append(f"  {label}: {count}")
    return lines
