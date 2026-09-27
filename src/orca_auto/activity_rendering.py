"""What ``queue list`` prints in text mode, as one structured table, and the clear lines."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from orca_auto import activity_labels, terminal
from orca_auto.core import statuses as _s
from orca_auto.core.utils import normalize_text, safe_int
from orca_auto.terminal_table import display_width, pad_right, truncate

EMPTY_QUEUE_MESSAGE = "No matching activities."

# Queue table columns in display order and their headers.
QUEUE_COLUMNS = ("status", "name", "detail", "id", "elapsed")
QUEUE_HEADERS = {
    "status": "Status",
    "name": "Name",
    "detail": "Detail",
    "id": "ID",
    "elapsed": "Elapsed",
}
# Gap rendered between adjacent table columns.
QUEUE_COLUMN_GAP = "  "
# Smallest width each flexible column may shrink to under terminal-width
# pressure, and the order in which columns surrender space (least essential
# first). ``id`` shrinks last because it doubles as the ``queue cancel`` target.
QUEUE_MIN_WIDTHS = {"detail": 6, "name": 8, "id": 8}
QUEUE_SHRINK_ORDER = ("detail", "name", "id")


@dataclass(frozen=True)
class QueueListTable:
    """Everything ``queue list`` prints in text mode, before TTY or plain styling.

    ``rows`` holds one rendered line per entry of ``activities``, in the same
    order, so the caller can tint each line by its row's status. ``header`` and
    ``divider`` are empty when there are no rows. ``notes`` are printed under
    the table, or under the empty message.
    """

    active_simulations: int
    header: str
    divider: str
    rows: tuple[str, ...]
    activities: tuple[dict[str, Any], ...]
    notes: tuple[str, ...]

    @property
    def summary(self) -> str:
        """The byte-stable line piped and scripted consumers parse."""
        return f"active_simulations: {self.active_simulations}"


def _fit_flexible_widths(widths: dict[str, int], *, max_total: int | None) -> dict[str, int]:
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


def _column_widths(prepared: Sequence[dict[str, str]], *, max_width: int | None) -> dict[str, int]:
    widths = {
        key: max(
            display_width(QUEUE_HEADERS[key]),
            max((display_width(row[key]) for row in prepared), default=0),
        )
        for key in QUEUE_COLUMNS
    }
    # Soft caps keep wide values from dominating before terminal-fit shrinking.
    widths["detail"] = max(display_width(QUEUE_HEADERS["detail"]), min(36, widths["detail"]))
    widths["name"] = max(display_width(QUEUE_HEADERS["name"]), min(32, widths["name"]))

    # ``status`` and ``elapsed`` are intrinsically narrow and fixed, so the
    # flexible text columns absorb any terminal-width shortfall.
    gap_width = display_width(QUEUE_COLUMN_GAP) * (len(QUEUE_COLUMNS) - 1)
    fixed_width = widths["status"] + widths["elapsed"] + gap_width
    widths.update(
        _fit_flexible_widths(
            {key: widths[key] for key in QUEUE_SHRINK_ORDER},
            max_total=None if max_width is None else max(0, max_width - fixed_width),
        )
    )
    return widths


def _render_row(values: dict[str, str], widths: dict[str, int]) -> str:
    return QUEUE_COLUMN_GAP.join(
        pad_right(truncate(values[key], max_width=widths[key]), widths[key])
        for key in QUEUE_COLUMNS
    )


def queue_list_table(payload: dict[str, Any], *, max_width: int | None) -> QueueListTable:
    """The ``queue list`` text for one listing payload, fitted to ``max_width``."""

    activities = tuple(payload.get("activities", []))
    active_simulations = int(payload.get("active_simulations", 0))
    blocker_lines = queue_admission_blocker_lines(payload.get("admission_blockers", []))
    if not activities:
        return QueueListTable(
            active_simulations=active_simulations,
            header="",
            divider="",
            rows=(),
            activities=(),
            notes=tuple(blocker_lines),
        )

    now = activity_labels.queue_table_now()
    prepared = [
        {
            "status": terminal.status_icon(item.get("status")),
            "name": activity_labels.queue_name_text(item),
            "detail": activity_labels.queue_detail_text(item),
            "id": normalize_text(item.get("activity_id")) or "-",
            "elapsed": activity_labels.queue_elapsed_text(item, now=now),
        }
        for item in activities
    ]
    widths = _column_widths(prepared, max_width=max_width)
    gap_width = display_width(QUEUE_COLUMN_GAP) * (len(QUEUE_COLUMNS) - 1)
    return QueueListTable(
        active_simulations=active_simulations,
        header=_render_row(QUEUE_HEADERS, widths),
        divider="─" * (sum(widths[key] for key in QUEUE_COLUMNS) + gap_width),
        rows=tuple(_render_row(row, widths) for row in prepared),
        activities=activities,
        # Printed under the table, where no column shrinking can truncate them:
        # the ``detail`` cell surrenders width first. Empty for every queue
        # without undrained cancels, a running or failed row's log, or a blocker.
        notes=(
            *queue_pending_cancel_lines(activities),
            *queue_worker_log_lines(activities),
            *blocker_lines,
        ),
    )


#: At most this many rows are named before the note falls back to a count.
_MAX_NAMED_PENDING_CANCEL_ROWS = 5


def queue_pending_cancel_lines(rows: Sequence[dict[str, Any]]) -> list[str]:
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
    for item in rows:
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


def queue_worker_log_lines(rows: Sequence[dict[str, Any]]) -> list[str]:
    """One ``worker_log:`` note per running or failed row that has a log.

    Printed under the table like the cancel note: the path would not survive
    the ``detail`` column's width cap, and a finished job's log is noise.
    """

    lines = []
    for item in rows:
        path = normalize_text(item.get("worker_log"))
        if path and _s.normalize_status(item.get("status")) in _s.WORKER_LOG_STATUSES:
            lines.append(f"worker_log: {normalize_text(item.get('activity_id')) or '-'} {path}")
    return lines


def queue_admission_blocker_lines(blockers: Sequence[dict[str, Any]]) -> list[str]:
    lines = []
    for blocker in blockers:
        lines.extend(
            [
                f"admission_blocked: ORCA queue {blocker['allowed_root']} (queue_id={blocker['queue_id']})",
                f"  {blocker['reason']}",
                f"  {blocker['next_action']}",
            ]
        )
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
        ("orca_queue_entries", "ORCA queue entries"),
        ("orca_run_states", "ORCA run states"),
    )
    for key, label in labels:
        count = int(cleared.get(key, 0) or 0)
        if count > 0:
            lines.append(f"  {label}: {count}")
    removed_logs = int(payload.get("removed_worker_logs", 0) or 0)
    if removed_logs > 0:
        lines.append(f"  worker logs removed: {removed_logs}")
    return lines
