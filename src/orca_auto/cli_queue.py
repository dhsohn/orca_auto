from __future__ import annotations

import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from orca_auto import activity_rendering as _activity_rendering
from orca_auto import cli_common, cli_style
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
from orca_auto.job_resource import live_job_pgids
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

    emit_queue_list_once: Callable[..., int] | None = None
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
_FAILED_STATUSES = frozenset(_s.FAILED_STATUSES | {"error"})
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
) -> list[str]:
    """Build the TTY summary band: a title line plus a status-count line."""

    counts = Counter(_summary_status_group(item.get("status")) for _indent, item in display_rows)
    segments: list[str] = []
    for key in _SUMMARY_ORDER:
        count = counts.get(key, 0)
        if count <= 0:
            continue
        label, representative = _SUMMARY_META[key]
        text = f"{activity_status_icon(representative)} {count} {label}"
        color = cli_style.status_color(representative)
        segments.append(cli_style.paint(text, color) if color else text)
    title = cli_style.paint(_QUEUE_RAIL, cli_style.CYAN) + cli_style.paint(
        "orca_auto queue", cli_style.BOLD
    )
    active_text = cli_style.paint(f"{int(active_simulations)} active", cli_style.BOLD)
    lines = [f"{title}   {active_text}"]
    if segments:
        lines.append("  " + cli_style.paint(" · ", cli_style.DIM).join(segments))
    return lines


def _watch_banner_line(spinner: str, interval: float, *, now: Any | None = None) -> str:
    # A non-interactive terminal keeps the historical plain banner byte-for-byte
    # (no spinner/clock), so piped, `--no-color`, and `FORCE_COLOR`-piped `--watch`
    # output is unchanged; the spinner and clock are interactive-only affordances.
    if not _layout_interactive():
        return f"orca_auto queue list — refresh every {interval:g}s · Ctrl-C to exit"
    clock = (now or _queue_table_now()).strftime("%H:%M:%S")
    left = cli_style.paint(f"{spinner} live", cli_style.CYAN) + cli_style.label(
        f" · refresh {interval:g}s · Ctrl-C to exit"
    )
    return f"{left}   {cli_style.label(clock)}"


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


def _resource_gauge_line(metrics: SystemMetrics) -> str | None:
    """Render the TTY system CPU/RAM/load gauge line, or ``None`` if empty.

    Each field is included only when the underlying ``/proc`` source was
    available, so a partial read (e.g. load without meminfo) still renders.
    """

    segments: list[str] = []
    if metrics.cpu_percent is not None:
        segments.append(
            f"{cli_style.label('CPU')} {_resource_bar(metrics.cpu_percent / 100.0)}"
            f" {metrics.cpu_percent:3.0f}%"
        )
    if metrics.mem_used_bytes is not None and metrics.mem_total_bytes:
        fraction = metrics.mem_used_bytes / metrics.mem_total_bytes
        used_gb = metrics.mem_used_bytes / 1024**3
        total_gb = metrics.mem_total_bytes / 1024**3
        segments.append(
            f"{cli_style.label('RAM')} {_resource_bar(fraction)} {used_gb:.1f}/{total_gb:.1f}G"
        )
    if metrics.load1 is not None and metrics.load5 is not None and metrics.load15 is not None:
        load = f"{metrics.load1:.2f} {metrics.load5:.2f} {metrics.load15:.2f}"
        segments.append(f"{cli_style.label('load')} {load}")
    if not segments:
        return None
    return "  " + "   ".join(segments)


def _default_job_metrics_provider() -> Callable[[str | None], dict[str, JobMetrics]]:
    """Build a stateful provider mapping ``queue_id`` to live per-job metrics.

    Holds one :class:`ProcessGroupSampler` so CPU% is a delta across watch
    refreshes. Returns ``{}`` when no job has a validated-live engine process.
    """

    sampler = ProcessGroupSampler()

    def provide(shared_config: str | None) -> dict[str, JobMetrics]:
        pgid_by_queue = live_job_pgids(shared_config)
        if not pgid_by_queue:
            return {}
        metrics = sampler.sample(pgid_by_queue.values(), now=time.monotonic())
        return {
            queue_id: metrics[pgid] for queue_id, pgid in pgid_by_queue.items() if pgid in metrics
        }

    return provide


def _fmt_rss(rss_bytes: int) -> str:
    if rss_bytes >= 1024**3:
        return f"{rss_bytes / 1024**3:.1f}G"
    if rss_bytes >= 1024**2:
        return f"{rss_bytes / 1024**2:.0f}M"
    return f"{max(0, rss_bytes) // 1024}K"


def _row_job_metric(item: dict[str, Any], job_metrics: dict[str, JobMetrics]) -> JobMetrics | None:
    # Only per-job rows carry engine metrics; guarding on kind keeps a workflow
    # parent whose id happens to collide with a queue id from being annotated
    # with a child's usage.
    if normalize_text(item.get("kind")).lower() != "job":
        return None
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    candidates = (
        item.get("activity_id"),
        metadata.get("queue_id"),
        metadata.get("run_id"),
        item.get("cancel_target"),
    )
    for candidate in candidates:
        key = normalize_text(candidate)
        if key and key in job_metrics:
            return job_metrics[key]
    return None


def _job_annotation(metrics: JobMetrics) -> str:
    parts: list[str] = []
    if metrics.cpu_percent is not None:
        parts.append(f"{cli_style.label('cpu')} {metrics.cpu_percent:.0f}%")
    parts.append(f"{cli_style.label('ram')} {_fmt_rss(metrics.rss_bytes)}")
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
    return _QueueListRequest(
        shared_config=_effective_shared_config_text(args) or None,
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
        workflow_root=_workflow_root_for_args(args),
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
        workflow_root=_workflow_root_for_args(args),
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
    max_width = term_width
    if tty and term_width is not None:
        max_width = max(0, term_width - rail_width)
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
            display_rows, active_simulations=presentation.active_simulations
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
                body = body + _job_annotation(metric)
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
    emit_once = (deps.emit_queue_list_once if deps else None) or _emit_queue_list_once
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
    # refreshes. Resolve the effective config once (discovering env/default) so
    # per-job metrics also appear in the no-argument `queue list --watch` flow,
    # where ``request.shared_config`` is None but a default config is discovered.
    job_metrics_provider = (deps.job_metrics_provider if deps else None) or (
        _default_job_metrics_provider() if interactive else None
    )
    metrics_config = cli_common._discover_shared_config_path(request.shared_config)
    tick = 0
    try:
        while True:
            if interactive:
                cli_style.clear_screen()
            print(_watch_banner_line(frames[tick % len(frames)], interval))
            if sampler is not None and interactive:
                metrics = sampler.sample()
                gauge = _resource_gauge_line(metrics) if metrics is not None else None
                if gauge:
                    print(gauge)
            job_metrics = (
                job_metrics_provider(metrics_config)
                if job_metrics_provider is not None and interactive
                else None
            )
            emit_once(args, request, job_metrics=job_metrics)
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
