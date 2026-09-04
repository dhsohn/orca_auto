from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from orca_auto import activity_labels as _activity_labels
from orca_auto import terminal_table as _terminal_table
from orca_auto.core.utils import normalize_text, safe_int


def _tree_prefixes(indents: Sequence[int]) -> list[str]:
    """Return box-drawing tree prefixes for a flat, depth-ordered row list.

    A row is the last child at its depth when no later row shares that depth
    before the indentation drops below it; ancestors that still have a later
    sibling contribute a ``│`` continuation bar. Depth-0 rows get no prefix, so
    the result lines up one-to-one with ``rows``.
    """

    normalized = [max(0, int(value)) for value in indents]
    count = len(normalized)

    def _has_later_row_at(start: int, depth: int) -> bool:
        for later in range(start, count):
            if normalized[later] < depth:
                return False
            if normalized[later] == depth:
                return True
        return False

    prefixes: list[str] = []
    for index, depth in enumerate(normalized):
        if depth <= 0:
            prefixes.append("")
            continue
        segments = [
            "│  " if _has_later_row_at(index + 1, level) else "   " for level in range(1, depth)
        ]
        segments.append("├─ " if _has_later_row_at(index + 1, depth) else "└─ ")
        prefixes.append("".join(segments))
    return prefixes


def _prepare_queue_table_rows(
    rows: Sequence[tuple[int, dict[str, Any]]],
    *,
    now: datetime | None = None,
    use_tree_glyphs: bool = False,
) -> list[dict[str, str]]:
    prepared: list[dict[str, str]] = []
    resolved_now = now or _activity_labels.queue_table_now()
    prefixes = _tree_prefixes([indent for indent, _ in rows]) if use_tree_glyphs else None
    for position, (indent, item) in enumerate(rows):
        name = _activity_labels.queue_name_text(item)
        if prefixes is not None:
            name = prefixes[position] + name
        elif int(indent) > 0:
            name = ("  " * int(indent)) + name
        item_id = normalize_text(item.get("activity_id")) or "-"
        prepared.append(
            {
                "status": _activity_labels.queue_status_icon(item),
                "name": name,
                "detail": _activity_labels.queue_detail_text(item),
                "id": item_id,
                "elapsed": _activity_labels.queue_elapsed_text(item, now=resolved_now),
            }
        )
    return prepared


def queue_table_lines(
    rows: Sequence[tuple[int, dict[str, Any]]],
    *,
    now: datetime | None = None,
    max_width: int | None = None,
    include_id: bool = True,
    use_tree_glyphs: bool = False,
) -> list[str]:
    prepared = _prepare_queue_table_rows(rows, now=now, use_tree_glyphs=use_tree_glyphs)
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
    use_tree_glyphs: bool = False,
) -> list[str]:
    lines = [f"active_simulations: {int(active_simulations)}"]
    if not rows:
        lines.append(empty_message)
        return lines
    lines.extend(
        queue_table_lines(
            rows,
            now=now,
            max_width=max_width,
            include_id=include_id,
            use_tree_glyphs=use_tree_glyphs,
        )
    )
    return lines


#: At most this many rows are named before the note falls back to a count.
_MAX_NAMED_PENDING_CANCEL_ROWS = 5


def queue_pending_cancel_lines(rows: Sequence[tuple[int, dict[str, Any]]]) -> list[str]:
    """Name the rows holding cancel transitions no worker has journaled yet.

    This is a note printed under the table rather than a cell inside it.
    ``detail`` is soft-capped at 36 columns and is first in
    ``QUEUE_SHRINK_ORDER``, so on an 80- or 100-column terminal a marker in
    that cell is truncated away — exactly the terminals an operator reads.
    Returns an empty list when no row is affected, so the byte output of a
    piped ``queue list`` is unchanged for every queue without one. A long list
    of ids wraps rather than truncating: the ids are the note's payload.
    """

    pending: list[tuple[str, int]] = []
    for _indent, item in rows:
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        count = safe_int(metadata.get("cancel_transitions_pending"), default=0)
        if count > 0:
            pending.append((normalize_text(item.get("activity_id")) or "-", count))
    if not pending:
        return []
    named = pending[:_MAX_NAMED_PENDING_CANCEL_ROWS]
    listed = ", ".join(f"{activity_id}={count}" for activity_id, count in named)
    if len(pending) > len(named):
        listed = f"{listed}, +{len(pending) - len(named)} more"
    return [
        f"cancel_pending: {listed}",
        "  undrained cancel transitions; `queue list clear` refuses these rows.",
    ]


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
