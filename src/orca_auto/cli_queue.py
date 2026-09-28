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
from typing import Any

from orca_auto import terminal, terminal_table
from orca_auto.activity import cancel_activity, clear_activities, list_activities
from orca_auto.activity_rendering import EMPTY_QUEUE_MESSAGE, queue_clear_lines, queue_list_table
from orca_auto.cli_handlers import CommandConfigError, resolve_command_config
from orca_auto.core import statuses as _s
from orca_auto.core.config.files import YAML_CONFIG_LOAD_EXCEPTIONS
from orca_auto.core.indexing import JobLocationIndexError
from orca_auto.core.queue import QueueStoreCorruptError
from orca_auto.core.utils import normalize_text
from orca_auto.terminal import emit_error, emit_json

_QUEUE_STATE_ERRORS: tuple[type[Exception], ...] = (
    *YAML_CONFIG_LOAD_EXCEPTIONS,
    QueueStoreCorruptError,
    JobLocationIndexError,
)
_CANCEL_TARGET_HINT = (
    "Check the configured runtime state, then run `orca_auto queue list` to see valid targets."
)


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


def _queue_header_band_lines(
    display_rows: Sequence[dict[str, Any]],
    *,
    active_simulations: int,
    max_width: int | None = None,
) -> list[str]:
    """Build the TTY summary band: a title line plus a status-count line."""

    counts = Counter(_s.summary_bucket(item.get("status")) for item in display_rows)
    segments: list[tuple[str, str | None]] = []
    for key, label, representative in _s.SUMMARY_BUCKETS:
        count = counts.get(key, 0)
        if count <= 0:
            continue
        text = f"{terminal.status_icon(representative)} {count} {label}"
        segments.append((text, terminal.status_color(representative)))

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


def _emit_queue_list_clear(payload: dict[str, Any], *, json_output: bool) -> int:
    if json_output:
        emit_json(payload)
        return 0
    for line in queue_clear_lines(payload):
        print(line)
    return 0


def _print_queue_list_text(*, payload: dict[str, Any]) -> int:
    tty = _layout_interactive()
    term_width = terminal_table.terminal_max_width()
    table = queue_list_table(payload, max_width=term_width)

    # Header: a styled summary band on a TTY, else the byte-stable
    # ``active_simulations: N`` line that piped and scripted consumers parse.
    if tty:
        for band_line in _queue_header_band_lines(
            table.activities,
            active_simulations=table.active_simulations,
            max_width=term_width,
        ):
            print(band_line)
    else:
        print(table.summary)

    if not table.rows:
        print(EMPTY_QUEUE_MESSAGE)
        for note in table.notes:
            print(note)
        return 0

    # Each data row is tinted by its status. On a non-TTY paint is a no-op, so
    # pipes and scripts get the plain table.
    if not tty:
        print(terminal.paint(table.header, terminal.BOLD))
        print(table.divider)
        for item, line in zip(table.activities, table.rows, strict=True):
            color = terminal.status_color(item.get("status"))
            print(terminal.paint(line, color) if color else line)
        for note in table.notes:
            print(note)
        return 0

    # On a TTY each row gains a status-colored left rail; the header and divider
    # are padded to match. If the table could not shrink far enough for the rail
    # (a very narrow terminal leaves it at its column floor), the rail would push
    # the block past the edge, so drop the rail rather than forcing a wrap.
    rail_width = terminal_table.display_width(_QUEUE_RAIL)
    table_width = terminal_table.display_width(table.divider)
    show_rail = term_width is None or table_width + rail_width <= term_width
    gutter = " " * rail_width if show_rail else ""
    print(gutter + terminal.paint(table.header, terminal.BOLD))
    print(gutter + terminal.paint(table.divider, terminal.DIM))
    for item, line in zip(table.activities, table.rows, strict=True):
        color = terminal.status_color(item.get("status"))
        body = terminal.paint(line, color) if color else line
        if show_rail:
            body = terminal.paint(_QUEUE_RAIL_GLYPH, color or terminal.DIM) + " " + body
        print(body)
    for note in table.notes:
        print(gutter + note)
    return 0


def _emit_queue_list_once(payload: dict[str, Any], *, json_output: bool) -> int:
    if json_output:
        emit_json(payload)
        return 0
    return _print_queue_list_text(payload=payload)


def cmd_queue_list(args: Any) -> int:
    json_output = bool(getattr(args, "json", False))
    try:
        # One config for the rows and the global active count.
        config = resolve_command_config(args)
    except CommandConfigError as exc:
        emit_error(exc, hint=exc.hint, json_output=json_output)
        return 1

    limit = int(getattr(args, "limit", 0) or 0)
    if normalize_text(getattr(args, "action", None)).lower() == "clear":
        if getattr(args, "status", None) or limit != 0:
            emit_error(
                "`orca_auto queue list clear` does not support --status/--limit filters.",
                json_output=json_output,
            )
            return 1
        try:
            clear_payload = clear_activities(config_path=config.path, runs_root=config.runs_root)
        except _QUEUE_STATE_ERRORS as exc:
            emit_error(
                exc,
                hint="Check the config path and repair the reported state file before retrying.",
                json_output=json_output,
            )
            return 1
        return _emit_queue_list_clear(clear_payload, json_output=json_output)

    try:
        # ``list_activities`` owns the status filter and the page; its payload is
        # rendered as is, so the count, rows and summaries can never disagree.
        payload = list_activities(
            config_path=config.path,
            runs_root=config.runs_root,
            limit=limit,
            statuses=getattr(args, "status", None) or (),
        )
    except _QUEUE_STATE_ERRORS as exc:
        emit_error(
            exc,
            hint="Check the config path and repair the reported state file before retrying.",
            json_output=json_output,
        )
        return 1
    return _emit_queue_list_once(payload, json_output=json_output)


def _emit_queue_cancel(payload: dict[str, Any], error: str, *, json_output: bool) -> int:
    if error:
        if json_output:
            # The resolved row, if any, stays beside the verdict.
            emit_json(payload, ok=False, error=error)
        hint = (
            _CANCEL_TARGET_HINT
            if payload["result"]["reason"] in {"target_not_found", "ambiguous"}
            else "Run `orca_auto queue list` to inspect the current target state."
        )
        emit_error(error, hint=hint)
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
    json_output = bool(getattr(args, "json", False))
    try:
        config = resolve_command_config(args)
    except CommandConfigError as exc:
        emit_error(exc, hint=exc.hint, json_output=json_output)
        return 1
    try:
        payload, error = cancel_activity(target=args.target, runs_root=config.runs_root)
    except _QUEUE_STATE_ERRORS as exc:
        emit_error(exc, hint=_CANCEL_TARGET_HINT, json_output=json_output)
        return 1

    return _emit_queue_cancel(payload, error, json_output=json_output)
