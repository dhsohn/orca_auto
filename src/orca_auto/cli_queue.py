from __future__ import annotations

import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from orca_auto import activity_rendering as _activity_rendering
from orca_auto import cli_common, cli_style, terminal_table
from orca_auto.activity_presenter import (
    QueueListPresentationDeps,
    QueueListPresentationRequest,
    queue_list_display_rows_for_request,
    queue_list_text_presentation,
)
from orca_auto.activity_view import (
    activity_counter_config_path,
    activity_with_parent_hint,
    count_global_active_simulations,
    filter_activity_items,
    normalize_activity_filter_values,
)
from orca_auto.cli_common import (
    _effective_shared_config_text,
    _workflow_root_for_args,
)
from orca_auto.cli_errors import emit_error
from orca_auto.core import statuses as _s
from orca_auto.core.activity_icons import activity_status_icon
from orca_auto.core.utils import normalize_text
from orca_auto.flow.activity import cancel_activity, clear_activities, list_activities
from orca_auto.job_resource import live_job_processes
from orca_auto.system_metrics import (
    JobMetrics,
    ProcessGroupSampler,
    SystemMetrics,
    SystemMetricsSampler,
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


@dataclass(frozen=True)
class QueueCliDeps:
    """Optional overrides for the `queue list --watch` loop (test seams)."""

    emit_queue_list_once: Callable[[Any, _QueueListRequest], int] | None = None
    sleep: Callable[[float], None] | None = None
    system_metrics_sampler: SystemMetricsSampler | None = None
    job_metrics_provider: Callable[[str | None], dict[str, JobMetrics]] | None = None


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


def _queue_table_now() -> Any:
    return _activity_rendering._queue_table_now()


def _queue_elapsed_text(item: dict[str, Any], *, now: Any | None = None) -> str:
    return _activity_rendering._queue_elapsed_text(item, now=now)


def _queue_display_width(value: str) -> int:
    return _activity_rendering._queue_display_width(value)


def _queue_terminal_width() -> int | None:
    return _activity_rendering._terminal_max_width()


def _stdout_isatty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _layout_interactive() -> bool:
    """Whether to use the interactive layout (summary band, tree glyphs, rail,
    watch redraw) instead of the machine-readable plain view.

    Layout changes require a real terminal, so ``FORCE_COLOR`` — which enables
    color on a pipe — never restructures piped output; ``--no-color``/``NO_COLOR``
    keep the plain view. Color painting still follows
    :func:`cli_style.color_enabled`.
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
            if max_width is None or _queue_display_width(plain) <= max_width
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
                and _queue_display_width(candidate_plain) > max_width
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
            if max_width is not None and _queue_display_width(plain) > max_width:
                # Only possible when one segment is wider than the terminal.
                # Keep a visible, bounded prefix instead of dropping the bucket.
                text, color = row[0]
                bounded = terminal_table.truncate(f"  {text}", max_width=max_width)
                lines.append(cli_style.paint(bounded, color) if color else bounded)
                continue
            styled = [cli_style.paint(text, color) if color else text for text, color in row]
            lines.append("  " + separator.join(styled))
    return lines


def _watch_banner_line(
    spinner: str,
    interval: float,
    *,
    now: Any | None = None,
    max_width: int | None = None,
) -> str:
    # A non-interactive terminal keeps the historical plain banner byte-for-byte
    # (no spinner/clock), so piped, `--no-color`, and `FORCE_COLOR`-piped `--watch`
    # output is unchanged; the spinner and clock are interactive-only affordances.
    if not _layout_interactive():
        return f"orca_auto queue list — refresh every {interval:g}s · Ctrl-C to exit"
    clock = (now or _queue_table_now()).strftime("%H:%M:%S")
    lead = cli_style.paint(f"{spinner} live", cli_style.CYAN)
    candidates = (
        (
            lead
            + cli_style.label(f" · refresh {interval:g}s · Ctrl-C to exit")
            + f"   {cli_style.label(clock)}",
            f"{spinner} live · refresh {interval:g}s · Ctrl-C to exit   {clock}",
        ),
        (
            lead + cli_style.label(f" · refresh {interval:g}s · Ctrl-C to exit"),
            f"{spinner} live · refresh {interval:g}s · Ctrl-C to exit",
        ),
        (
            lead + cli_style.label(f" · {interval:g}s · Ctrl-C"),
            f"{spinner} live · {interval:g}s · Ctrl-C",
        ),
        (
            lead + cli_style.label(f" · {interval:g}s"),
            f"{spinner} live · {interval:g}s",
        ),
    )
    for styled, plain in candidates:
        if max_width is None or _queue_display_width(plain) <= max_width:
            return styled
    return cli_style.paint(
        terminal_table.truncate(
            f"{spinner} live",
            max_width=max(0, max_width or 0),
        ),
        cli_style.CYAN,
    )


def _resource_bar(fraction: float, *, width: int = 12) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    if fraction >= 0.9:
        color = cli_style.RED
    elif fraction >= 0.7:
        color = cli_style.YELLOW
    else:
        color = cli_style.GREEN
    return cli_style.paint("█" * filled, color) + cli_style.paint(
        "░" * (width - filled), cli_style.DIM
    )


def _resource_gauge_segments(
    metrics: SystemMetrics,
    *,
    bars: bool,
) -> list[tuple[str, str]]:
    """Return ``(styled, plain)`` resource segments for width fitting."""

    segments: list[tuple[str, str]] = []
    if metrics.cpu_percent is not None:
        value = f"{metrics.cpu_percent:3.0f}%"
        if bars:
            fraction = max(0.0, min(1.0, metrics.cpu_percent / 100.0))
            filled = round(fraction * 12)
            plain_bar = "█" * filled + "░" * (12 - filled)
            segments.append(
                (
                    f"{cli_style.label('CPU')} {_resource_bar(fraction)} {value}",
                    f"CPU {plain_bar} {value}",
                )
            )
        else:
            segments.append((f"{cli_style.label('CPU')} {value}", f"CPU {value}"))
    if metrics.mem_used_bytes is not None and metrics.mem_total_bytes:
        fraction = metrics.mem_used_bytes / metrics.mem_total_bytes
        used_gb = metrics.mem_used_bytes / 1024**3
        total_gb = metrics.mem_total_bytes / 1024**3
        value = f"{used_gb:.1f}/{total_gb:.1f}G"
        if bars:
            normalized = max(0.0, min(1.0, fraction))
            filled = round(normalized * 12)
            plain_bar = "█" * filled + "░" * (12 - filled)
            segments.append(
                (
                    f"{cli_style.label('RAM')} {_resource_bar(fraction)} {value}",
                    f"RAM {plain_bar} {value}",
                )
            )
        else:
            segments.append((f"{cli_style.label('RAM')} {value}", f"RAM {value}"))
    if metrics.load1 is not None and metrics.load5 is not None and metrics.load15 is not None:
        value = f"{metrics.load1:.2f} {metrics.load5:.2f} {metrics.load15:.2f}"
        segments.append((f"{cli_style.label('load')} {value}", f"load {value}"))
    return segments


def _resource_gauge_line(
    metrics: SystemMetrics,
    *,
    max_width: int | None = None,
) -> str | None:
    """Render the TTY system CPU/RAM/load gauge line, or ``None`` if empty.

    Each field is included only when the underlying ``/proc`` source was
    available, so a partial read (e.g. load without meminfo) still renders.
    """

    full = _resource_gauge_segments(metrics, bars=True)
    compact = _resource_gauge_segments(metrics, bars=False)
    if not full:
        return None

    def render(parts: Sequence[tuple[str, str]]) -> tuple[str, str]:
        return (
            "  " + "   ".join(styled for styled, _plain in parts),
            "  " + "   ".join(plain for _styled, plain in parts),
        )

    candidates = [
        full,
        compact,
        [part for part in full if not part[1].startswith("load ")],
        [part for part in compact if not part[1].startswith("load ")],
        *[[part] for part in compact],
    ]
    seen: set[str] = set()
    for parts in candidates:
        if not parts:
            continue
        styled, plain = render(parts)
        if plain in seen:
            continue
        seen.add(plain)
        if max_width is None or _queue_display_width(plain) <= max_width:
            return styled
    return None


def _default_job_metrics_provider() -> Callable[[str | None], dict[str, JobMetrics]]:
    """Build a stateful provider mapping ``queue_id`` to live per-job metrics.

    Holds one :class:`ProcessGroupSampler` so CPU% is a delta across watch
    refreshes. Returns ``{}`` when no job has a validated-live engine process.
    """

    sampler = ProcessGroupSampler()

    def provide(shared_config: str | None) -> dict[str, JobMetrics]:
        identities = live_job_processes(shared_config)
        metrics = sampler.sample(
            {identity: identity.pgid for identity in identities},
            now=time.monotonic(),
        )
        # Revalidate after the /proc scan. A leader that exited or changed
        # identity during sampling is omitted instead of attributing a newly
        # reused numeric process group to the prior queue row.
        validated_after_scan = set(live_job_processes(shared_config)) if identities else set()
        return {
            identity.queue_id: metrics[identity]
            for identity in identities
            if identity in metrics and identity in validated_after_scan
        }

    return provide


def _fmt_rss(rss_bytes: int) -> str:
    if rss_bytes >= 1024**3:
        return f"{rss_bytes / 1024**3:.1f}G"
    if rss_bytes >= 1024**2:
        return f"{rss_bytes / 1024**2:.0f}M"
    return f"{max(0, rss_bytes) // 1024}K"


def _row_job_metric(item: dict[str, Any], job_metrics: dict[str, JobMetrics]) -> JobMetrics | None:
    # Bind only a currently-running job row to its canonical queue id. Aliases
    # are intentionally ignored: a run/workflow id collision must never attach
    # another queue's process metrics, and a terminal transition between the
    # process sample and activity read must drop the stale annotation.
    if (
        normalize_text(item.get("kind")).lower() != "job"
        or _s.normalize_status(item.get("status")) != _s.STATUS_RUNNING
    ):
        return None
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    queue_id = normalize_text(metadata.get("queue_id"))
    return job_metrics.get(queue_id) if queue_id else None


def _job_annotation(metrics: JobMetrics, *, styled: bool = True) -> str:
    parts: list[str] = []
    cpu_label = cli_style.label("cpu") if styled else "cpu"
    ram_label = cli_style.label("ram") if styled else "ram"
    if metrics.cpu_percent is not None:
        parts.append(f"{cpu_label} {metrics.cpu_percent:.0f}%")
    parts.append(f"{ram_label} {_fmt_rss(metrics.rss_bytes)}")
    return "  " + "  ".join(parts)


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
        now=now or _queue_table_now(),
        max_width=max_width if max_width is not None else _queue_terminal_width(),
        include_id=include_id,
        empty_message=empty_message,
        use_tree_glyphs=_layout_interactive(),
    )


def _queue_clear_lines(payload: dict[str, Any]) -> list[str]:
    return _activity_rendering.queue_clear_lines(payload)


def _queue_list_presentation_request(
    request: _QueueListRequest,
    *,
    visible_items: Sequence[dict[str, Any]],
    active_simulations: int | None = None,
    now: Any | None = None,
    max_width: int | None = None,
) -> QueueListPresentationRequest:
    return QueueListPresentationRequest(
        visible_items=visible_items,
        config_hints=(request.shared_config,),
        prefer_config_hints=True,
        default_visible_items=request.default_combined_text_view,
        limit=request.limit,
        show_workflow_context=set(request.kind_values) != {"job"},
        visible_workflow_child_engines=("orca",) if request.default_combined_text_view else None,
        active_simulations=active_simulations,
        now=now,
        max_width=max_width,
    )


def _queue_list_request(args: Any) -> _QueueListRequest:
    explicit_config = _effective_shared_config_text(args) or None
    return _QueueListRequest(
        # Resolve one effective config up front so the activity rows, global
        # active count, and per-job metrics can never discover different repo
        # defaults in an alternate editable checkout.
        shared_config=cli_common._discover_shared_config_path(explicit_config),
        limit=int(getattr(args, "limit", 0) or 0),
        engine_values=normalize_activity_filter_values(getattr(args, "engine", None)),
        status_values=normalize_activity_filter_values(getattr(args, "status", None)),
        kind_values=normalize_activity_filter_values(getattr(args, "kind", None)),
        json_output=bool(getattr(args, "json", False)),
    )


def _cmd_queue_list_clear(args: Any, request: _QueueListRequest) -> int:
    if (
        any(getattr(args, field, None) for field in ("engine", "status", "kind"))
        or request.limit > 0
    ):
        emit_error(
            "`orca_auto queue list clear` does not support "
            "--engine/--status/--kind/--limit filters."
        )
        return 1

    payload = clear_activities(
        workflow_root=_workflow_root_for_args(args, config_path=request.shared_config),
        crest_config=request.shared_config,
        xtb_config=request.shared_config,
        orca_config=request.shared_config,
    )
    if request.json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0
    for line in _queue_clear_lines(payload):
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
        child_job_engines=() if request.default_combined_text_view else None,
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


def _queue_list_display_rows(
    *,
    payload: dict[str, Any],
    filtered_activities: Sequence[dict[str, Any]],
    request: _QueueListRequest,
) -> list[tuple[int, dict[str, Any]]]:
    return queue_list_display_rows_for_request(
        payload,
        request=_queue_list_presentation_request(
            request,
            visible_items=filtered_activities,
        ),
    )


def _print_queue_list_text(
    *,
    payload: dict[str, Any],
    filtered_payload: dict[str, Any],
    filtered_activities: Sequence[dict[str, Any]],
    request: _QueueListRequest,
    job_metrics: dict[str, JobMetrics] | None = None,
) -> int:
    tty = _layout_interactive()
    term_width = _queue_terminal_width()
    rail_width = _queue_display_width(_QUEUE_RAIL)
    annotation_width = 0
    if tty and job_metrics:
        annotation_width = max(
            (
                _queue_display_width(_job_annotation(metric, styled=False))
                for item in filtered_activities
                if (metric := _row_job_metric(item, job_metrics)) is not None
            ),
            default=0,
        )
    max_width = term_width
    if tty and term_width is not None:
        max_width = max(0, term_width - rail_width - annotation_width)
    presentation = queue_list_text_presentation(
        payload,
        request=_queue_list_presentation_request(
            request,
            visible_items=filtered_activities,
            active_simulations=filtered_payload["active_simulations"],
            now=_queue_table_now(),
            max_width=max_width,
        ),
        deps=QueueListPresentationDeps(
            queue_list_text_lines=_queue_list_text_lines,
        ),
    )
    display_rows = presentation.display_rows
    lines = presentation.lines

    # Header: a styled summary band on a TTY, else the byte-stable
    # ``active_simulations: N`` line that piped/scripted/`--json` consumers parse.
    if tty:
        for band_line in _queue_header_band_lines(
            display_rows,
            active_simulations=presentation.active_simulations,
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
    table_width = _queue_display_width(lines[2])
    show_rail = term_width is None or table_width + rail_width <= term_width
    gutter = " " * rail_width if show_rail else ""
    print(gutter + cli_style.paint(lines[1], cli_style.BOLD))
    print(gutter + cli_style.paint(lines[2], cli_style.DIM))
    for (_indent, item), line in zip(display_rows, lines[3:], strict=True):
        color = cli_style.status_color(item.get("status"))
        body = cli_style.paint(line, color) if color else line
        if show_rail:
            body = cli_style.paint(_QUEUE_RAIL_GLYPH, color or cli_style.DIM) + " " + body
        if job_metrics:
            metric = _row_job_metric(item, job_metrics)
            if metric is not None:
                annotated = body + _job_annotation(metric)
                if term_width is None or (
                    _queue_display_width(line)
                    + (rail_width if show_rail else 0)
                    + _queue_display_width(_job_annotation(metric, styled=False))
                    <= term_width
                ):
                    body = annotated
        print(body)
    return 0


def _emit_queue_list_once(
    args: Any,
    request: _QueueListRequest,
    *,
    job_metrics: dict[str, JobMetrics] | None = None,
) -> int:
    payload = _queue_list_payload(args, request)
    filtered_payload, filtered_activities = _filtered_queue_payload(payload, request)
    if request.json_output:
        print(json.dumps(filtered_payload, ensure_ascii=True, indent=2))
        return 0
    return _print_queue_list_text(
        payload=payload,
        filtered_payload=filtered_payload,
        filtered_activities=filtered_activities,
        request=request,
        job_metrics=job_metrics,
    )


def _watch_queue_list(
    args: Any,
    request: _QueueListRequest,
    *,
    deps: QueueCliDeps | None = None,
) -> int:
    interval = max(0.5, float(getattr(args, "interval", 2.0) or 2.0))
    custom_emit_once = deps.emit_queue_list_once if deps else None
    sleep = (deps.sleep if deps else None) or time.sleep
    frames = cli_style.SPINNER_FRAMES
    interactive = _layout_interactive()
    # The sampler, the per-job provider, and the resource line are all
    # interactive-only, so piped/`--no-color`/`FORCE_COLOR`-piped watch output
    # keeps the plain banner and table and never grows a resource line. The
    # sampler holds the previous /proc snapshot so CPU% is a delta over the
    # refresh interval.
    sampler = (deps.system_metrics_sampler if deps else None) or (
        SystemMetricsSampler() if interactive else None
    )
    # The provider carries its own ProcessGroupSampler so CPU% is a delta across
    # refreshes. The request already carries the one discovered effective config
    # shared by activity collection, active counting, and metric attribution.
    job_metrics_provider = None
    if interactive and custom_emit_once is None:
        job_metrics_provider = (deps.job_metrics_provider if deps else None) or (
            _default_job_metrics_provider()
        )
    metrics_config = request.shared_config
    tick = 0
    try:
        while True:
            if interactive:
                cli_style.clear_screen()
            print(
                _watch_banner_line(
                    frames[tick % len(frames)],
                    interval,
                    max_width=_queue_terminal_width(),
                )
            )
            if sampler is not None and interactive:
                metrics = sampler.sample()
                gauge = (
                    _resource_gauge_line(metrics, max_width=_queue_terminal_width())
                    if metrics is not None
                    else None
                )
                if gauge:
                    print(gauge)
            job_metrics = (
                job_metrics_provider(metrics_config)
                if job_metrics_provider is not None and interactive
                else None
            )
            if custom_emit_once is not None:
                # Preserve the released two-argument test-seam contract. The
                # production renderer alone consumes the new metrics payload.
                custom_emit_once(args, request)
            else:
                _emit_queue_list_once(args, request, job_metrics=job_metrics)
            sleep(interval)
            tick += 1
    except KeyboardInterrupt:
        print()
        return 0


def cmd_queue_list(args: Any, *, deps: QueueCliDeps | None = None) -> int:
    request = _queue_list_request(args)
    if bool(getattr(args, "watch", False)) and request.json_output:
        emit_error("orca_auto queue list --watch does not support --json.")
        return 1
    if normalize_text(getattr(args, "action", None)).lower() == "clear":
        return _cmd_queue_list_clear(args, request)

    if bool(getattr(args, "watch", False)) and not request.json_output:
        return _watch_queue_list(args, request, deps=deps)

    return _emit_queue_list_once(args, request)


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
    except (LookupError, ValueError, TimeoutError) as exc:
        emit_error(exc, hint="Run `orca_auto queue list` to see valid targets.")
        return 1

    if bool(getattr(args, "json", False)):
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
