"""``orca_auto queue list|cancel``: the operator surface over the activity catalog.

Text output is a status-tinted table on a TTY and a byte-stable plain table on
a pipe; ``--json`` prints the listing/clear/cancel payload through the shared
``emit_json`` document. Failures reach stderr as ``error:`` lines and, under
``--json``, stdout as ``{"ok": false, "error": ...}``.
"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto import activity_labels, terminal, terminal_table
from orca_auto import activity_rendering as _activity_rendering
from orca_auto.activity import cancel_activity, clear_activities, list_activities
from orca_auto.activity_labels import activity_status_icon
from orca_auto.activity_view import normalize_activity_filter_values
from orca_auto.core import statuses as _s
from orca_auto.core.activity_index import ActivityIndexError
from orca_auto.core.config import discovery
from orca_auto.core.config.discovery import (
    shared_config_text_from_args,
)
from orca_auto.core.config.files import YAML_CONFIG_LOAD_EXCEPTIONS, shared_runs_root_from_config
from orca_auto.core.indexing import JobLocationIndexError
from orca_auto.core.queue import QueueStoreCorruptError
from orca_auto.core.utils import normalize_text
from orca_auto.terminal import emit_error, emit_json

_QUEUE_STATE_ERRORS: tuple[type[Exception], ...] = (
    ActivityIndexError,
    *YAML_CONFIG_LOAD_EXCEPTIONS,
    QueueStoreCorruptError,
    JobLocationIndexError,
)
_QUEUE_CANCEL_ERRORS: tuple[type[Exception], ...] = (LookupError, *_QUEUE_STATE_ERRORS)


@dataclass(frozen=True)
class _QueueListRequest:
    shared_config: str | None
    limit: int
    status_values: tuple[str, ...]
    json_output: bool


def _stdout_isatty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _layout_interactive() -> bool:
    """Whether stdout can use the styled human layout.

    Layout changes require both a real terminal and enabled ANSI styling.
    ``FORCE_COLOR`` on a pipe may paint text but cannot enable the human layout.
    """

    return terminal.color_enabled() and _stdout_isatty()


# The queue table gains a status-colored left rail on a TTY; the rail glyph plus
# its trailing space are reserved from the terminal width so the fitted table and
# the rail together never exceed the terminal.
_QUEUE_RAIL_GLYPH = "▎"
_QUEUE_RAIL = _QUEUE_RAIL_GLYPH + " "

# Disjoint summary groups for the TTY header band. Each bucket maps to one
# representative status for its icon/color; the ``pending`` bucket intentionally
# aggregates the queued-like statuses (submitted/retrying/planned/…) under the
# queued glyph, so a bucket glyph illustrates its group rather than matching every
# row in it exactly.
_PENDING_STATUSES = frozenset(_s.QUEUE_ACTIVE_STATUSES - {_s.STATUS_RUNNING})
# Bucket keys reuse the representative status name where one exists.
_GROUP_RUNNING = _s.STATUS_RUNNING
_GROUP_PENDING = _s.STATUS_PENDING
_GROUP_DONE = "done"
_GROUP_FAILED = _s.STATUS_FAILED
_GROUP_CANCELLED = _s.STATUS_CANCELLED
_GROUP_OTHER = "other"
_SUMMARY_ORDER = (
    _GROUP_RUNNING,
    _GROUP_PENDING,
    _GROUP_DONE,
    _GROUP_FAILED,
    _GROUP_CANCELLED,
    _GROUP_OTHER,
)
_SUMMARY_META: dict[str, tuple[str, str]] = {
    _GROUP_RUNNING: ("running", _s.STATUS_RUNNING),
    _GROUP_PENDING: ("queued", _s.STATUS_QUEUED),
    _GROUP_DONE: ("done", _s.STATUS_COMPLETED),
    _GROUP_FAILED: ("failed", _s.STATUS_FAILED),
    _GROUP_CANCELLED: ("cancelled", _s.STATUS_CANCELLED),
    _GROUP_OTHER: ("other", _s.STATUS_UNKNOWN),
}


def _summary_status_group(status: object) -> str:
    normalized = _s.normalize_status(status)
    if normalized == _s.STATUS_RUNNING:
        return _GROUP_RUNNING
    if normalized in _PENDING_STATUSES:
        return _GROUP_PENDING
    if normalized == _s.STATUS_COMPLETED:
        return _GROUP_DONE
    if normalized in _s.FAILED_STATUSES:
        return _GROUP_FAILED
    if normalized == _s.STATUS_CANCELLED:
        return _GROUP_CANCELLED
    return _GROUP_OTHER


def _queue_header_band_lines(
    display_rows: Sequence[tuple[int, dict[str, Any]]],
    *,
    active_simulations: int,
    max_width: int | None = None,
) -> list[str]:
    """Build the TTY summary band: a title line plus a status-count line."""

    counts = Counter(_summary_status_group(item.get("status")) for _indent, item in display_rows)
    segments: list[tuple[str, str | None]] = []
    for key in _SUMMARY_ORDER:
        count = counts.get(key, 0)
        if count <= 0:
            continue
        label, representative = _SUMMARY_META[key]
        text = f"{activity_status_icon(representative)} {count} {label}"
        color = terminal.status_color(representative)
        segments.append((text, color))

    active_plain = f"{int(active_simulations)} active"
    title_candidates = (
        (
            terminal.paint(_QUEUE_RAIL, terminal.CYAN)
            + terminal.paint("orca_auto queue", terminal.BOLD)
            + "   "
            + terminal.paint(active_plain, terminal.BOLD),
            f"{_QUEUE_RAIL}orca_auto queue   {active_plain}",
        ),
        (
            terminal.paint(_QUEUE_RAIL, terminal.CYAN)
            + terminal.paint("queue", terminal.BOLD)
            + "   "
            + terminal.paint(active_plain, terminal.BOLD),
            f"{_QUEUE_RAIL}queue   {active_plain}",
        ),
        (terminal.paint(active_plain, terminal.BOLD), active_plain),
    )
    title = next(
        (
            styled
            for styled, plain in title_candidates
            if max_width is None or terminal_table.display_width(plain) <= max_width
        ),
        terminal.paint(
            terminal_table.truncate(active_plain, max_width=max(0, max_width or 0)),
            terminal.BOLD,
        ),
    )
    lines = [title]
    if segments:
        rows: list[list[tuple[str, str | None]]] = []
        current: list[tuple[str, str | None]] = []
        for segment in segments:
            candidate = [*current, segment]
            candidate_plain = "  " + " · ".join(text for text, _color in candidate)
            if (
                current
                and max_width is not None
                and terminal_table.display_width(candidate_plain) > max_width
            ):
                rows.append(current)
                current = [segment]
            else:
                current = candidate
        if current:
            rows.append(current)

        separator = terminal.paint(" · ", terminal.DIM)
        for row in rows:
            plain = "  " + " · ".join(text for text, _color in row)
            if max_width is not None and terminal_table.display_width(plain) > max_width:
                # Only possible when one segment is wider than the terminal.
                # Keep a visible, bounded prefix instead of dropping the bucket.
                text, color = row[0]
                bounded = terminal_table.truncate(f"  {text}", max_width=max_width)
                lines.append(terminal.paint(bounded, color) if color else bounded)
                continue
            styled = [terminal.paint(text, color) if color else text for text, color in row]
            lines.append("  " + separator.join(styled))
    return lines


def _queue_list_text_lines(
    rows: Sequence[tuple[int, dict[str, Any]]],
    *,
    active_simulations: int,
    now: Any | None = None,
    max_width: int | None = None,
    include_id: bool = True,
    empty_message: str = "No matching activities.",
) -> list[str]:
    return _activity_rendering.queue_list_text_lines(
        rows,
        active_simulations=active_simulations,
        now=now or activity_labels.queue_table_now(),
        max_width=max_width if max_width is not None else terminal_table.terminal_max_width(),
        include_id=include_id,
        empty_message=empty_message,
        use_tree_glyphs=_layout_interactive(),
    )


def _queue_list_request(args: Any) -> _QueueListRequest:
    explicit_config = shared_config_text_from_args(args) or None
    return _QueueListRequest(
        # Resolve one effective config up front so activity rows and the global
        # active count use the same checkout and runtime roots.
        shared_config=discovery.resolve_shared_config_path(explicit_config),
        limit=int(getattr(args, "limit", 0) or 0),
        status_values=normalize_activity_filter_values(getattr(args, "status", None)),
        json_output=bool(getattr(args, "json", False)),
    )


def _queue_list_clear_payload(args: Any, request: _QueueListRequest) -> dict[str, Any]:
    return clear_activities(
        orca_config=request.shared_config,
    )


def _emit_queue_list_clear(payload: dict[str, Any], *, json_output: bool) -> int:
    if json_output:
        emit_json(payload)
        return 0
    for line in _activity_rendering.queue_clear_lines(payload):
        print(line)
    return 0


def _missing_runs_root(args: Any, request: _QueueListRequest) -> str | None:
    """The configured runs root when it is not a directory, else None."""
    root = shared_runs_root_from_config(request.shared_config)
    if not root:
        return None
    return None if Path(root).is_dir() else str(root)


def _queue_list_payload(args: Any, request: _QueueListRequest) -> dict[str, Any]:
    # ``list_activities`` owns the status filter and the page; its payload is
    # rendered as is, so the count, rows and summaries can never disagree.
    return list_activities(
        limit=request.limit,
        statuses=request.status_values,
        refresh=bool(getattr(args, "refresh", False)),
        orca_config=request.shared_config,
    )


def _print_queue_list_text(*, payload: dict[str, Any]) -> int:
    tty = _layout_interactive()
    term_width = terminal_table.terminal_max_width()
    rail_width = terminal_table.display_width(_QUEUE_RAIL)
    display_rows = [(0, item) for item in payload.get("activities", [])]
    active_simulations = int(payload.get("active_simulations", 0))
    lines = _queue_list_text_lines(
        display_rows,
        active_simulations=active_simulations,
        now=activity_labels.queue_table_now(),
        max_width=term_width,
    )

    # Header: a styled summary band on a TTY, else the byte-stable
    # ``active_simulations: N`` line that piped/scripted/`--json` consumers parse.
    if tty:
        for band_line in _queue_header_band_lines(
            display_rows,
            active_simulations=active_simulations,
            max_width=term_width,
        ):
            print(band_line)
    else:
        print(lines[0])

    blocker_lines = _activity_rendering.queue_admission_blocker_lines(
        payload.get("admission_blockers", [])
    )
    if not display_rows:
        print(lines[1])
        for note in blocker_lines:
            print(note)
        return 0

    # Printed under the table, where no column shrinking can truncate it: the
    # ``detail`` cell surrenders width first, so a narrow terminal would hide
    # the one thing that explains an unclearable row. Empty for every queue
    # without such a row.
    pending_cancel_lines = [
        *_activity_rendering.queue_pending_cancel_lines(display_rows),
        *_activity_rendering.queue_worker_log_lines(display_rows),
        *blocker_lines,
    ]

    # lines[1] is the header, lines[2] the divider, and the rest map one-to-one
    # onto display_rows so each data row is tinted by its status. On a non-TTY
    # this stays byte-for-byte identical to the historical output (paint is a
    # no-op) except for those trailing note lines, so pipes/scripts are
    # unaffected unless a row holds undrained cancel transitions or a
    # running/failed row names its worker log.
    if not tty:
        print(terminal.paint(lines[1], terminal.BOLD))
        print(lines[2])
        for (_indent, item), line in zip(display_rows, lines[3:], strict=True):
            color = terminal.status_color(item.get("status"))
            print(terminal.paint(line, color) if color else line)
        for note in pending_cancel_lines:
            print(note)
        return 0

    # On a TTY each row gains a status-colored left rail; the header and divider
    # are padded to match. The table was fit to ``term_width - rail_width``; if it
    # could not shrink that far (a very narrow terminal leaves it at its column
    # floor), the rail would push the block past the edge, so drop the rail and
    # keep the historical width rather than forcing a wrap.
    table_width = terminal_table.display_width(lines[2])
    show_rail = term_width is None or table_width + rail_width <= term_width
    gutter = " " * rail_width if show_rail else ""
    print(gutter + terminal.paint(lines[1], terminal.BOLD))
    print(gutter + terminal.paint(lines[2], terminal.DIM))
    for (_indent, item), line in zip(display_rows, lines[3:], strict=True):
        color = terminal.status_color(item.get("status"))
        body = terminal.paint(line, color) if color else line
        if show_rail:
            body = terminal.paint(_QUEUE_RAIL_GLYPH, color or terminal.DIM) + " " + body
        print(body)
    for note in pending_cancel_lines:
        print(gutter + note)
    return 0


def _emit_queue_list_once(payload: dict[str, Any], request: _QueueListRequest) -> int:
    if request.json_output:
        emit_json(payload)
        return 0
    return _print_queue_list_text(payload=payload)


def cmd_queue_list(args: Any) -> int:
    json_output = bool(getattr(args, "json", False))
    try:
        request = _queue_list_request(args)
    except _QUEUE_STATE_ERRORS as exc:
        emit_error(
            exc,
            hint="Check the config path and repair the reported state file before retrying.",
            json_output=json_output,
        )
        return 1

    missing_root = _missing_runs_root(args, request)
    if missing_root is not None:
        emit_error(
            f"runs_root does not exist: {missing_root}",
            hint="Check runs_root in the config; a typo here would otherwise list as an empty queue.",
            json_output=json_output,
        )
        return 1

    if normalize_text(getattr(args, "action", None)).lower() == "clear":
        if getattr(args, "status", None) or request.limit != 0:
            emit_error(
                "`orca_auto queue list clear` does not support --status/--limit filters.",
                json_output=json_output,
            )
            return 1
        try:
            clear_payload = _queue_list_clear_payload(args, request)
        except _QUEUE_STATE_ERRORS as exc:
            emit_error(
                exc,
                hint="Check the config path and repair the reported state file before retrying.",
                json_output=json_output,
            )
            return 1
        try:
            return _emit_queue_list_clear(clear_payload, json_output=request.json_output)
        except BrokenPipeError:
            return 0

    try:
        payload = _queue_list_payload(args, request)
    except _QUEUE_STATE_ERRORS as exc:
        emit_error(
            exc,
            hint="Check the config path and repair the reported state file before retrying.",
            json_output=json_output,
        )
        return 1
    try:
        return _emit_queue_list_once(payload, request)
    except BrokenPipeError:
        return 0


def _emit_queue_cancel(payload: dict[str, Any], *, json_output: bool) -> int:
    result = payload.get("result", {})
    if result.get("returncode", 0) != 0 or payload.get("status") == _s.STATUS_FAILED:
        message = (
            normalize_text(result.get("stderr"))
            or normalize_text(result.get("reason"))
            or "Cancellation failed."
        )
        if json_output:
            # The target's payload still describes what was found; keep it
            # beside the verdict rather than replacing it with a bare error.
            emit_json(payload, ok=False, error=message)
        emit_error(message, hint="Run `orca_auto queue list` to inspect the current target state.")
        return 1
    if json_output:
        emit_json(payload)
        return 0

    print(f"{terminal.label('activity_id:')} {payload.get('activity_id', '-')}")
    print(f"{terminal.label('kind:')} {payload.get('kind', '-')}")
    print(f"{terminal.label('engine:')} {payload.get('engine', '-')}")
    print(f"{terminal.label('source:')} {payload.get('source', '-')}")
    print(f"{terminal.label('label:')} {payload.get('label', '-')}")
    print(f"{terminal.label('status:')} {terminal.status_text(payload.get('status', '-'))}")
    print(f"{terminal.label('cancel_target:')} {payload.get('cancel_target', '-')}")
    return 0


def cmd_queue_cancel(args: Any) -> int:
    shared_config = shared_config_text_from_args(args) or None
    json_output = bool(getattr(args, "json", False))
    try:
        payload = cancel_activity(
            target=args.target,
            orca_config=shared_config,
        )
    except _QUEUE_CANCEL_ERRORS as exc:
        emit_error(
            exc,
            hint=(
                "Check the configured runtime state, then run `orca_auto queue list` "
                "to see valid targets."
            ),
            json_output=json_output,
        )
        return 1

    try:
        return _emit_queue_cancel(payload, json_output=json_output)
    except BrokenPipeError:
        return 0
