from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from orca_auto import activity_labels, cli_common, cli_style, terminal_table
from orca_auto import activity_rendering as _activity_rendering
from orca_auto.activity_view import (
    activity_counter_config_path,
    activity_with_parent_hint,
    count_global_active_simulations,
    filter_activity_items,
    normalize_activity_filter_values,
    queue_list_default_visible_items,
    queue_list_display_rows,
)
from orca_auto.cli_common import (
    _effective_shared_config_text,
    _workflow_root_for_args,
)
from orca_auto.cli_errors import emit_error
from orca_auto.core import statuses as _s
from orca_auto.core.activity_icons import activity_status_icon
from orca_auto.core.config.bounded_yaml import YAML_CONFIG_LOAD_EXCEPTIONS
from orca_auto.core.indexing import JobLocationIndexError
from orca_auto.core.queue import QueueStoreCorruptError
from orca_auto.core.utils import normalize_text
from orca_auto.flow.activity import cancel_activity, clear_activities, list_activities
from orca_auto.flow.registry import WorkflowRegistryCorruptError

_QUEUE_STATE_ERRORS = (
    *YAML_CONFIG_LOAD_EXCEPTIONS,
    QueueStoreCorruptError,
    JobLocationIndexError,
    WorkflowRegistryCorruptError,
)


@dataclass(frozen=True)
class _QueueListRequest:
    shared_config: str | None
    limit: int
    engine_values: tuple[str, ...]
    status_values: tuple[str, ...]
    kind_values: tuple[str, ...]
    json_output: bool

    @property
    def default_combined_text_view(self) -> bool:
        return (
            not self.json_output
            and not self.engine_values
            and not self.status_values
            and not self.kind_values
        )


def _activity_counter_config_path(
    *,
    payload: dict[str, Any],
    config_hint: str | None,
) -> str | None:
    return activity_counter_config_path(
        payload,
        config_hints=(config_hint,),
        prefer_hints=True,
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

    return cli_style.color_enabled() and _stdout_isatty()


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
_FAILED_STATUSES = frozenset(_s.FAILED_STATUSES | {"error", _s.STATUS_REPAIR_BLOCKED})
_SUMMARY_ORDER = ("running", "pending", "done", "failed", "cancelled", "other")
_SUMMARY_META: dict[str, tuple[str, str]] = {
    "running": ("running", _s.STATUS_RUNNING),
    "pending": ("queued", _s.STATUS_QUEUED),
    "done": ("done", _s.STATUS_COMPLETED),
    "failed": ("failed", _s.STATUS_FAILED),
    "cancelled": ("cancelled", _s.STATUS_CANCELLED),
    "other": ("other", _s.STATUS_UNKNOWN),
}


def _summary_status_group(status: object) -> str:
    normalized = _s.normalize_status(status)
    if normalized == _s.STATUS_RUNNING:
        return "running"
    if normalized in _PENDING_STATUSES:
        return "pending"
    if normalized == _s.STATUS_COMPLETED:
        return "done"
    if normalized in _FAILED_STATUSES:
        return "failed"
    if normalized == _s.STATUS_CANCELLED:
        return "cancelled"
    return "other"


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
        color = cli_style.status_color(representative)
        segments.append((text, color))

    active_plain = f"{int(active_simulations)} active"
    title_candidates = (
        (
            cli_style.paint(_QUEUE_RAIL, cli_style.CYAN)
            + cli_style.paint("orca_auto queue", cli_style.BOLD)
            + "   "
            + cli_style.paint(active_plain, cli_style.BOLD),
            f"{_QUEUE_RAIL}orca_auto queue   {active_plain}",
        ),
        (
            cli_style.paint(_QUEUE_RAIL, cli_style.CYAN)
            + cli_style.paint("queue", cli_style.BOLD)
            + "   "
            + cli_style.paint(active_plain, cli_style.BOLD),
            f"{_QUEUE_RAIL}queue   {active_plain}",
        ),
        (cli_style.paint(active_plain, cli_style.BOLD), active_plain),
    )
    title = next(
        (
            styled
            for styled, plain in title_candidates
            if max_width is None or terminal_table.display_width(plain) <= max_width
        ),
        cli_style.paint(
            terminal_table.truncate(active_plain, max_width=max(0, max_width or 0)),
            cli_style.BOLD,
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

        separator = cli_style.paint(" · ", cli_style.DIM)
        for row in rows:
            plain = "  " + " · ".join(text for text, _color in row)
            if max_width is not None and terminal_table.display_width(plain) > max_width:
                # Only possible when one segment is wider than the terminal.
                # Keep a visible, bounded prefix instead of dropping the bucket.
                text, color = row[0]
                bounded = terminal_table.truncate(f"  {text}", max_width=max_width)
                lines.append(cli_style.paint(bounded, color) if color else bounded)
                continue
            styled = [cli_style.paint(text, color) if color else text for text, color in row]
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
    explicit_config = _effective_shared_config_text(args) or None
    return _QueueListRequest(
        # Resolve one effective config up front so activity rows and the global
        # active count use the same checkout and runtime roots.
        shared_config=cli_common._discover_shared_config_path(explicit_config),
        limit=int(getattr(args, "limit", 0) or 0),
        engine_values=normalize_activity_filter_values(getattr(args, "engine", None)),
        status_values=normalize_activity_filter_values(getattr(args, "status", None)),
        kind_values=normalize_activity_filter_values(getattr(args, "kind", None)),
        json_output=bool(getattr(args, "json", False)),
    )


def _queue_list_clear_payload(args: Any, request: _QueueListRequest) -> dict[str, Any]:
    return clear_activities(
        workflow_root=_workflow_root_for_args(args, config_path=request.shared_config),
        crest_config=request.shared_config,
        xtb_config=request.shared_config,
        orca_config=request.shared_config,
    )


def _emit_queue_list_clear(payload: dict[str, Any], *, json_output: bool) -> int:
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0
    for line in _activity_rendering.queue_clear_lines(payload):
        print(line)
    return 0


def _queue_list_payload(args: Any, request: _QueueListRequest) -> dict[str, Any]:
    return list_activities(
        workflow_root=_workflow_root_for_args(args, config_path=request.shared_config),
        limit=0,
        refresh=bool(getattr(args, "refresh", False)),
        crest_config=request.shared_config,
        xtb_config=request.shared_config,
        orca_config=request.shared_config,
    )


def _filtered_queue_payload(
    payload: dict[str, Any],
    request: _QueueListRequest,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    activities = filter_activity_items(
        payload.get("activities", []),
        engines=request.engine_values,
        statuses=request.status_values,
        kinds=request.kind_values,
    )
    limited_activities = activities[: request.limit] if request.limit > 0 else list(activities)
    active_simulations = count_global_active_simulations(
        payload.get("activities", []),
        config_path=_activity_counter_config_path(
            payload=payload, config_hint=request.shared_config
        ),
    )
    return {
        "count": len(limited_activities),
        "active_simulations": active_simulations,
        "activities": [activity_with_parent_hint(item) for item in limited_activities],
        "sources": dict(payload.get("sources", {})),
    }, activities


def _print_queue_list_text(
    *,
    payload: dict[str, Any],
    filtered_payload: dict[str, Any],
    filtered_activities: Sequence[dict[str, Any]],
    request: _QueueListRequest,
) -> int:
    tty = _layout_interactive()
    term_width = terminal_table.terminal_max_width()
    rail_width = terminal_table.display_width(_QUEUE_RAIL)
    display_items = list(filtered_activities)
    if request.default_combined_text_view:
        display_items = queue_list_default_visible_items(display_items)
    if request.limit > 0:
        display_items = display_items[: request.limit]
    display_rows = queue_list_display_rows(
        all_items=list(payload.get("activities", [])),
        visible_items=display_items,
        show_workflow_context=set(request.kind_values) != {"job"},
    )
    active_simulations = filtered_payload["active_simulations"]
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

    if not display_rows:
        print(lines[1])
        return 0

    # lines[1] is the header, lines[2] the divider, and the rest map one-to-one
    # onto display_rows so each data row is tinted by its status. On a non-TTY
    # this stays byte-for-byte identical to the historical output (paint is a
    # no-op), so pipes/scripts are unaffected.
    if not tty:
        print(cli_style.paint(lines[1], cli_style.BOLD))
        print(lines[2])
        for (_indent, item), line in zip(display_rows, lines[3:], strict=True):
            color = cli_style.status_color(item.get("status"))
            print(cli_style.paint(line, color) if color else line)
        return 0

    # On a TTY each row gains a status-colored left rail; the header and divider
    # are padded to match. The table was fit to ``term_width - rail_width``; if it
    # could not shrink that far (a very narrow terminal leaves it at its column
    # floor), the rail would push the block past the edge, so drop the rail and
    # keep the historical width rather than forcing a wrap.
    table_width = terminal_table.display_width(lines[2])
    show_rail = term_width is None or table_width + rail_width <= term_width
    gutter = " " * rail_width if show_rail else ""
    print(gutter + cli_style.paint(lines[1], cli_style.BOLD))
    print(gutter + cli_style.paint(lines[2], cli_style.DIM))
    for (_indent, item), line in zip(display_rows, lines[3:], strict=True):
        color = cli_style.status_color(item.get("status"))
        body = cli_style.paint(line, color) if color else line
        if show_rail:
            body = cli_style.paint(_QUEUE_RAIL_GLYPH, color or cli_style.DIM) + " " + body
        print(body)
    return 0


def _emit_queue_list_once(
    payload: dict[str, Any],
    filtered_payload: dict[str, Any],
    filtered_activities: Sequence[dict[str, Any]],
    request: _QueueListRequest,
) -> int:
    if request.json_output:
        print(json.dumps(filtered_payload, ensure_ascii=True, indent=2))
        return 0
    return _print_queue_list_text(
        payload=payload,
        filtered_payload=filtered_payload,
        filtered_activities=filtered_activities,
        request=request,
    )


def cmd_queue_list(args: Any) -> int:
    try:
        request = _queue_list_request(args)
    except _QUEUE_STATE_ERRORS as exc:
        emit_error(
            exc,
            hint="Check the config path and repair the reported state file before retrying.",
        )
        return 1

    if normalize_text(getattr(args, "action", None)).lower() == "clear":
        if (
            any(getattr(args, field, None) for field in ("engine", "status", "kind"))
            or request.limit > 0
        ):
            emit_error(
                "`orca_auto queue list clear` does not support "
                "--engine/--status/--kind/--limit filters."
            )
            return 1
        try:
            clear_payload = _queue_list_clear_payload(args, request)
        except _QUEUE_STATE_ERRORS as exc:
            emit_error(
                exc,
                hint="Check the config path and repair the reported state file before retrying.",
            )
            return 1
        try:
            return _emit_queue_list_clear(clear_payload, json_output=request.json_output)
        except BrokenPipeError:
            return 0

    try:
        payload = _queue_list_payload(args, request)
        filtered_payload, filtered_activities = _filtered_queue_payload(payload, request)
    except _QUEUE_STATE_ERRORS as exc:
        emit_error(
            exc,
            hint="Check the config path and repair the reported state file before retrying.",
        )
        return 1
    try:
        return _emit_queue_list_once(
            payload,
            filtered_payload,
            filtered_activities,
            request,
        )
    except BrokenPipeError:
        return 0


def _emit_queue_cancel(payload: dict[str, Any], *, json_output: bool) -> int:
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    print(f"{cli_style.label('activity_id:')} {payload.get('activity_id', '-')}")
    print(f"{cli_style.label('kind:')} {payload.get('kind', '-')}")
    print(f"{cli_style.label('engine:')} {payload.get('engine', '-')}")
    print(f"{cli_style.label('source:')} {payload.get('source', '-')}")
    print(f"{cli_style.label('label:')} {payload.get('label', '-')}")
    print(f"{cli_style.label('status:')} {cli_style.status_text(payload.get('status', '-'))}")
    print(f"{cli_style.label('cancel_target:')} {payload.get('cancel_target', '-')}")
    return 0


def cmd_queue_cancel(args: Any) -> int:
    shared_config = _effective_shared_config_text(args) or None
    try:
        payload = cancel_activity(
            target=args.target,
            workflow_root=_workflow_root_for_args(args),
            crest_config=shared_config,
            xtb_config=shared_config,
            orca_config=shared_config,
        )
    except (LookupError, *_QUEUE_STATE_ERRORS) as exc:
        emit_error(
            exc,
            hint=(
                "Check the configured runtime state, then run `orca_auto queue list` "
                "to see valid targets."
            ),
        )
        return 1

    try:
        return _emit_queue_cancel(payload, json_output=bool(getattr(args, "json", False)))
    except BrokenPipeError:
        return 0
