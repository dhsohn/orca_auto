"""IRC job report: path profile, endpoint summary, and validation SI block."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..completion_rules import IRC_ROUTE_RE
from ..input_blocks import file_route_lines
from ..parser import KCAL_PER_HARTREE, OrcaResult, parse_opt_progress, parse_orca_output
from ..parser.io import read_orca_text
from .attempts import (
    AttemptReportRow,
    attempt_dicts,
    attempt_report_rows,
    attempts_table_html,
    duration_text,
    final_out_path,
    terminal_actions_html,
)
from .frequencies import ModeSummary, find_frequency_analysis, mode_section_html, mode_summaries
from .render import (
    ChartSeries,
    ReportComponent,
    job_meta_html,
    line_chart_svg,
    metric_card,
    status_badges,
)
from .settings import ReportSetting, match_dotted_setting, settings_table_html

# The IRC driver announces itself with a banner and prints its settings as
# dotted rows up to the first blank line; there is no "IRC settings" header.
_IRC_SETTINGS_HEADER_RE = re.compile(r"Intrinsic Reaction Coordinate Calculation", re.IGNORECASE)
# Direction banners are boxed in asterisks, so the token sits mid-line.
_IRC_DIRECTION_RE = re.compile(r"\b(FORWARD|BACKWARD)\s+IRC\b")
# Iteration rows: iteration, E(Eh), dE(kcal/mol), max(|G|), RMS(G).
_IRC_ITERATION_RE = re.compile(
    r"^\s*(\d+)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s*$"
)
_IRC_PATH_SUMMARY_HEADER_RE = re.compile(r"\bIRC\s+PATH\s+SUMMARY\b", re.IGNORECASE)
_IRC_PATH_ROW_RE = re.compile(
    r"^\s*(TS|[-+]?\d+)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)"
    r"(?:\s+<=\s*([A-Za-z-]+))?\s*$",
    re.IGNORECASE,
)
_SECTION_HEADER_RE = re.compile(
    r"\b(?:FORWARD|BACKWARD)\s+IRC\b|\bIRC\s+PATH\s+SUMMARY\b|"
    r"\bCARTESIAN\s+COORDINATES\b|\bFINAL\s+SINGLE\s+POINT\s+ENERGY\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IrcIterationPoint:
    direction: str
    iteration: int
    energy_hartree: float
    delta_e_kcal: float
    max_gradient: float
    rms_gradient: float


@dataclass(frozen=True)
class IrcPathPoint:
    label: str
    order: int
    step: int | None
    energy_hartree: float
    relative_kcal: float
    max_gradient: float
    rms_gradient: float
    marker: str


@dataclass(frozen=True)
class IrcParsedOutput:
    settings: tuple[ReportSetting, ...]
    iterations: tuple[IrcIterationPoint, ...]
    path_points: tuple[IrcPathPoint, ...]
    irc_marker_found: bool


@dataclass(frozen=True)
class IrcReportData:
    title: str
    job_id: str
    status: str
    reason: str
    route_line: str
    formula: str
    method: str
    basis_set: str
    orca_version: str
    started_at: str
    finished_at: str
    total_duration_text: str
    attempts: tuple[AttemptReportRow, ...]
    settings: tuple[ReportSetting, ...]
    iterations: tuple[IrcIterationPoint, ...]
    path_points: tuple[IrcPathPoint, ...]
    irc_marker_found: bool
    optimization_steps: tuple[tuple[int, float], ...]
    optimization_converged: bool
    result: OrcaResult | None
    imaginary_count: int | None
    mode_summaries: tuple[ModeSummary, ...]
    last_out_name: str


@dataclass(frozen=True)
class IrcSiBlock:
    name: str
    route_line: str
    orca_version: str
    settings: tuple[ReportSetting, ...]
    path_points: tuple[IrcPathPoint, ...]
    last_out_name: str


class IrcReportError(Exception):
    """The IRC report cannot be assembled because required artifacts are missing."""


def input_uses_irc(inp_path: Path) -> bool:
    return bool(IRC_ROUTE_RE.search(" ".join(file_route_lines(inp_path))))


def parse_irc_output(out_path: Path) -> IrcParsedOutput:
    text = read_orca_text(str(out_path))
    return IrcParsedOutput(
        settings=_parse_irc_settings(text),
        iterations=_parse_irc_iterations(text),
        path_points=_parse_irc_path_summary(text),
        irc_marker_found=bool(
            _IRC_PATH_SUMMARY_HEADER_RE.search(text) or "IRC-DRV" in text.upper()
        ),
    )


def collect_irc_report_data(
    reaction_dir: Path,
    state: Mapping[str, Any],
) -> IrcReportData | None:
    selected_raw = str(state.get("selected_inp") or "").strip()
    if not selected_raw:
        return None
    selected_inp = Path(selected_raw)
    if not input_uses_irc(selected_inp):
        return None

    route_lines = file_route_lines(selected_inp)
    attempts = attempt_dicts(state)
    rows = _irc_attempt_rows(attempts)
    parsed = _latest_parsed_irc_output(attempts)
    optimization_steps, optimization_converged = _latest_opt_progress(attempts)

    out_path = final_out_path(state)
    result: OrcaResult | None = None
    if out_path is not None:
        try:
            result = parse_orca_output(str(out_path))
        except OSError:
            result = None
    analysis, _frequency_attempt_index = find_frequency_analysis(attempts)

    final_result = state.get("final_result")
    final_payload: Mapping[str, Any] = final_result if isinstance(final_result, Mapping) else {}
    last_out = str(final_payload.get("last_out_path") or "").strip()
    if not last_out and attempts:
        last_out = str(attempts[-1].get("out_path") or "").strip()

    return IrcReportData(
        title=reaction_dir.name or str(reaction_dir),
        job_id=str(state.get("job_id") or ""),
        status=str(state.get("status") or ""),
        reason=str(final_payload.get("reason") or ""),
        route_line=" ".join(route_lines),
        formula=result.formula if result is not None else "",
        method=result.method if result is not None else "",
        basis_set=result.basis_set if result is not None else "",
        orca_version=result.orca_version if result is not None else "",
        started_at=str(state.get("started_at") or ""),
        finished_at=str(final_payload.get("completed_at") or ""),
        total_duration_text=duration_text(
            state.get("started_at"), final_payload.get("completed_at")
        ),
        attempts=rows,
        settings=parsed.settings,
        iterations=parsed.iterations,
        path_points=parsed.path_points,
        irc_marker_found=parsed.irc_marker_found,
        optimization_steps=optimization_steps,
        optimization_converged=optimization_converged,
        result=result,
        imaginary_count=analysis.imaginary_count() if analysis is not None else None,
        mode_summaries=mode_summaries(analysis, None) if analysis is not None else (),
        last_out_name=Path(last_out).name if last_out else "",
    )


def collect_irc_si_block(reaction_dir: Path, state: Mapping[str, Any]) -> IrcSiBlock | None:
    if str(state.get("status") or "") != "completed":
        return None
    selected_raw = str(state.get("selected_inp") or "").strip()
    if not selected_raw:
        return None
    selected_inp = Path(selected_raw)
    route_lines = file_route_lines(selected_inp)
    if not route_lines:
        raise IrcReportError(f"cannot read route lines from input {selected_inp}")
    if not IRC_ROUTE_RE.search(" ".join(route_lines)):
        return None

    out_path = final_out_path(state)
    if out_path is None:
        raise IrcReportError(f"no output file found for {reaction_dir}")
    parsed = parse_irc_output(out_path)
    try:
        result = parse_orca_output(str(out_path))
    except OSError:
        result = None
    return IrcSiBlock(
        name=reaction_dir.name,
        route_line=" ".join(route_lines),
        orca_version=result.orca_version if result is not None else "",
        settings=parsed.settings,
        path_points=parsed.path_points,
        last_out_name=out_path.name,
    )


def render_irc_si_block_md(block: IrcSiBlock) -> str:
    version_note = f"        (ORCA {block.orca_version})" if block.orca_version else ""
    lines = [
        f"== {block.name} ==",
        f"{block.route_line}{version_note}",
        "IRC validation summary",
    ]
    ts = _path_marker(block.path_points, "TS")
    endpoint_1, endpoint_2 = _path_endpoints(block.path_points)
    if block.path_points:
        lines.append(f"Parsed IRC path points = {len(block.path_points)}")
    if ts is not None:
        lines.append(
            f"TS step = {ts.label}; E = {ts.energy_hartree:.6f} Eh; "
            f"relative ΔE = {ts.relative_kcal:+.2f} kcal mol⁻¹"
        )
    for label, point in (("path endpoint 1", endpoint_1), ("path endpoint 2", endpoint_2)):
        if point is None:
            continue
        endpoint_text = (
            f"{label}: step {point.label}; E = {point.energy_hartree:.6f} Eh; "
            f"relative ΔE = {point.relative_kcal:+.2f} kcal mol⁻¹"
        )
        if ts is not None:
            endpoint_text += f"; from TS = {point.relative_kcal - ts.relative_kcal:+.2f} kcal mol⁻¹"
        lines.append(endpoint_text)

    trajectory_settings = [
        setting for setting in block.settings if "trajectory" in setting.label.lower()
    ]
    for setting in trajectory_settings:
        lines.append(f"{setting.label}: {setting.value}")
    if block.last_out_name:
        lines.append(f"Last output: {block.last_out_name}")
    lines.append(
        "⚠ IRC endpoints are path endpoints, not fully optimized stationary structures; "
        "optimize endpoints before publishing endpoint coordinates."
    )
    lines.append("")
    return "\n".join(lines)


def irc_report_badges(data: IrcReportData) -> tuple[tuple[str, str], ...]:
    badges = status_badges(data.status, data.reason)
    if data.irc_marker_found:
        badges.append(("IRC path found", "ok"))
    return tuple(badges)


def irc_report_meta_html(data: IrcReportData) -> str:
    formula_text = f" &#183; {html.escape(data.formula)}" if data.formula else ""
    version_text = f" &#183; ORCA {html.escape(data.orca_version)}" if data.orca_version else ""
    return job_meta_html(
        route_line=data.route_line,
        job_id=data.job_id,
        started_at=data.started_at,
        finished_at=data.finished_at,
        extra_html=formula_text + version_text,
    )


def irc_report_component(
    data: IrcReportData,
    *,
    include_attempt_metric: bool = True,
    include_attempt_chain: bool = True,
    include_common_summary: bool = True,
    include_common_metric: bool = True,
    include_optimization: bool = True,
    include_vibrational: bool = True,
) -> ReportComponent:
    sections: list[tuple[str, str]] = []
    if include_common_summary:
        common_html = _calculation_summary_html(data)
        if common_html:
            sections.append(("Calculation summary", common_html))
    if include_optimization:
        opt_html = _optimization_section_html(data)
        if opt_html:
            sections.append((_optimization_section_title(data), opt_html))
    if include_vibrational and data.mode_summaries:
        sections.append(("Vibrational summary", mode_section_html(data.mode_summaries, None)))
    chart = _path_chart_svg(data) or (
        '<p class="muted">No IRC path-summary points were parsed from the attempt outputs.</p>'
    )
    sections.append(("IRC path profile", chart + _path_table_html(data.path_points)))
    settings_html = settings_table_html(data.settings)
    if settings_html:
        sections.append(("IRC setup", settings_html))
    iteration_html = _iterations_table_html(data.iterations)
    if iteration_html:
        sections.append(("IRC iterations", iteration_html))
    if include_attempt_chain:
        attempts_html = attempts_table_html(data.attempts, "Detail") + terminal_actions_html(
            data.attempts
        )
        sections.append(("Attempt chain", attempts_html))
    return ReportComponent(
        metrics_html=_metric_cards(
            data,
            include_attempts=include_attempt_metric,
            include_common=include_common_metric,
            include_optimization=include_optimization,
            include_vibrational=include_vibrational,
        ),
        sections=tuple(sections),
    )


def _parse_irc_settings(text: str) -> tuple[ReportSetting, ...]:
    settings: list[ReportSetting] = []
    in_settings = False
    for line in text.splitlines():
        if not in_settings:
            if _IRC_SETTINGS_HEADER_RE.search(line):
                in_settings = True
            continue
        if _SECTION_HEADER_RE.search(line):
            break
        if not line.strip():
            # The settings block (System/Settings/Tolerances/trajectory rows)
            # runs without blank lines; the first blank after content ends it.
            if settings:
                break
            continue
        setting = match_dotted_setting(line)
        if setting is not None and setting not in settings:
            settings.append(setting)
    return tuple(settings)


def _parse_irc_iterations(text: str) -> tuple[IrcIterationPoint, ...]:
    direction = ""
    table_started = False
    points: list[IrcIterationPoint] = []
    for line in text.splitlines():
        direction_match = _IRC_DIRECTION_RE.search(line)
        if direction_match is not None:
            direction = direction_match.group(1)
            table_started = False
            continue
        if not direction:
            continue
        if _IRC_PATH_SUMMARY_HEADER_RE.search(line):
            direction = ""
            table_started = False
            continue
        match = _IRC_ITERATION_RE.match(line)
        if match is None and table_started and line.strip():
            direction = ""
            table_started = False
            continue
        if match is None:
            continue
        table_started = True
        points.append(
            IrcIterationPoint(
                direction=direction,
                iteration=int(match.group(1)),
                energy_hartree=float(match.group(2)),
                delta_e_kcal=float(match.group(3)),
                max_gradient=float(match.group(4)),
                rms_gradient=float(match.group(5)),
            )
        )
    return tuple(points)


def _parse_irc_path_summary(text: str) -> tuple[IrcPathPoint, ...]:
    points: list[IrcPathPoint] = []
    in_summary = False
    for line in text.splitlines():
        if _IRC_PATH_SUMMARY_HEADER_RE.search(line):
            in_summary = True
            points = []
            continue
        if not in_summary:
            continue
        match = _IRC_PATH_ROW_RE.match(line)
        if match is None:
            if points and not line.strip():
                in_summary = False
            continue
        label = match.group(1).upper()
        step = None if label == "TS" else int(label)
        marker = (match.group(6) or "").upper()
        points.append(
            IrcPathPoint(
                label=label,
                order=len(points),
                step=step,
                energy_hartree=float(match.group(2)),
                relative_kcal=float(match.group(3)),
                max_gradient=float(match.group(4)),
                rms_gradient=float(match.group(5)),
                marker=marker,
            )
        )
    return tuple(points)


def _irc_attempt_rows(attempts: Sequence[Mapping[str, Any]]) -> tuple[AttemptReportRow, ...]:
    rows = list(attempt_report_rows(attempts, "initial IRC"))
    detailed: list[AttemptReportRow] = []
    for row, attempt in zip(rows, attempts, strict=True):
        parsed = _parse_attempt_irc_output(attempt)
        detail = ""
        if parsed is not None:
            parts = []
            if parsed.path_points:
                parts.append(f"{len(parsed.path_points)} path pts")
            if parsed.iterations:
                parts.append(f"{len(parsed.iterations)} IRC iter")
            detail = ", ".join(parts)
        detailed.append(
            AttemptReportRow(
                index=row.index,
                label=row.label,
                direction=row.direction,
                analyzer_status=row.analyzer_status,
                analyzer_reason=row.analyzer_reason,
                duration_text=row.duration_text,
                detail=detail,
                terminal_actions=row.terminal_actions,
            )
        )
    return tuple(detailed)


def _parse_attempt_irc_output(attempt: Mapping[str, Any]) -> IrcParsedOutput | None:
    out_raw = str(attempt.get("out_path") or "").strip()
    if not out_raw:
        return None
    out_path = Path(out_raw)
    if not out_path.exists():
        return None
    try:
        return parse_irc_output(out_path)
    except (OSError, FileNotFoundError):
        return None


def _latest_parsed_irc_output(attempts: Sequence[Mapping[str, Any]]) -> IrcParsedOutput:
    """Latest attempt output that actually contains IRC data.

    A retry that died before the IRC driver started (or a trailing Freq-only
    attempt) parses to an empty result; skipping such shells keeps the report
    consistent with the per-attempt detail column instead of masking an
    earlier attempt's parsed path.
    """
    fallback: IrcParsedOutput | None = None
    for attempt in reversed(attempts):
        parsed = _parse_attempt_irc_output(attempt)
        if parsed is None:
            continue
        if parsed.path_points or parsed.iterations or parsed.settings:
            return parsed
        if fallback is None:
            fallback = parsed
    if fallback is not None:
        return fallback
    return IrcParsedOutput(settings=(), iterations=(), path_points=(), irc_marker_found=False)


def _latest_opt_progress(
    attempts: Sequence[Mapping[str, Any]],
) -> tuple[tuple[tuple[int, float], ...], bool]:
    for attempt in reversed(attempts):
        out_raw = str(attempt.get("out_path") or "").strip()
        if not out_raw or not Path(out_raw).exists():
            continue
        try:
            progress = parse_opt_progress(out_raw)
        except (OSError, FileNotFoundError):
            continue
        steps = tuple((step.cycle, step.energy_hartree) for step in progress.steps)
        if steps:
            return steps, progress.is_converged
    return (), False


def _path_x(point: IrcPathPoint) -> float:
    return float(point.step if point.step is not None else point.order)


def _path_marker(points: Sequence[IrcPathPoint], marker: str) -> IrcPathPoint | None:
    marker = marker.upper()
    for point in points:
        if point.marker == marker or point.label == marker:
            return point
    return None


def _path_endpoints(
    points: Sequence[IrcPathPoint],
) -> tuple[IrcPathPoint | None, IrcPathPoint | None]:
    if not points:
        return None, None
    if len(points) == 1:
        return points[0], None
    return points[0], points[-1]


def _path_chart_svg(data: IrcReportData) -> str:
    if len(data.path_points) < 2:
        return ""
    series = [
        ChartSeries(
            label="IRC path",
            color="#2f6fb2",
            dash="",
            points=tuple((_path_x(point), point.relative_kcal) for point in data.path_points),
        )
    ]
    endpoint_1, endpoint_2 = _path_endpoints(data.path_points)
    for endpoint, label, color in (
        (endpoint_1, "path endpoint 1", "#69707c"),
        (endpoint_2, "path endpoint 2", "#3d4451"),
    ):
        if endpoint is not None:
            series.append(
                ChartSeries(
                    label=label,
                    color=color,
                    dash="",
                    points=((_path_x(endpoint), endpoint.relative_kcal),),
                )
            )
    ts = _path_marker(data.path_points, "TS")
    if ts is not None:
        series.append(
            ChartSeries(
                label="TS marker",
                color="#d97706",
                dash="",
                points=((_path_x(ts), ts.relative_kcal),),
            )
        )
    return line_chart_svg(
        tuple(series),
        x_label="IRC step",
        y_label="ΔE / kcal mol⁻¹",
        x_tick_fmt=".0f",
    )


def _is_ts_route(route_line: str) -> bool:
    return bool(re.search(r"\b(?:OPTTS|SCANTS|(?:ZOOM-)?NEB-TS)\b", route_line, re.IGNORECASE))


def _optimization_section_title(data: IrcReportData) -> str:
    return (
        "TS optimization convergence"
        if _is_ts_route(data.route_line)
        else "Optimization convergence"
    )


def _optimization_chart_svg(data: IrcReportData) -> str:
    if len(data.optimization_steps) < 2:
        return ""
    e_min = min(energy for _cycle, energy in data.optimization_steps)
    points = tuple(
        (float(cycle), (energy - e_min) * KCAL_PER_HARTREE)
        for cycle, energy in data.optimization_steps
    )
    return line_chart_svg(
        (ChartSeries(label="", color="#2f6fb2", dash="", points=points),),
        x_label="optimization cycle",
        y_label="ΔE / kcal mol⁻¹",
        x_tick_fmt=".0f",
    )


def _optimization_section_html(data: IrcReportData) -> str:
    if not data.optimization_steps:
        return ""
    chart = _optimization_chart_svg(data)
    final_cycle, final_energy = data.optimization_steps[-1]
    status = "converged" if data.optimization_converged else "not converged"
    note = (
        '<p class="muted">'
        f"Parsed {len(data.optimization_steps)} optimization cycles; "
        f"cycle {final_cycle} ended at {final_energy:.6f} Eh ({status})."
        "</p>"
    )
    return chart + note if chart else note


def _calculation_summary_html(data: IrcReportData) -> str:
    rows: list[tuple[str, str]] = []
    level = "/".join(part for part in (data.method, data.basis_set) if part)
    if level:
        rows.append(("Level", level))
    if data.formula:
        rows.append(("Formula", data.formula))
    if data.result is not None:
        rows.append(
            (
                "Charge / multiplicity",
                f"{data.result.charge} / {data.result.multiplicity}",
            )
        )
        if data.result.energy_hartree is not None:
            rows.append(("Final energy", f"{data.result.energy_hartree:.8f} Eh"))
    if not rows:
        return ""
    return (
        "<table><tbody>"
        + "".join(
            f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>"
            for label, value in rows
        )
        + "</tbody></table>"
    )


def _metric_cards(
    data: IrcReportData,
    *,
    include_attempts: bool = True,
    include_common: bool = True,
    include_optimization: bool = True,
    include_vibrational: bool = True,
) -> str:
    cards = []
    if data.path_points:
        cards.append(
            metric_card("IRC path points", str(len(data.path_points)), "parsed summary rows")
        )
    ts = _path_marker(data.path_points, "TS")
    if ts is not None:
        cards.append(
            metric_card(
                "TS path marker",
                f"{ts.energy_hartree:.6f} <small>Eh</small>",
                f"step {ts.label}, dE {ts.relative_kcal:+.2f} kcal/mol",
            )
        )
    endpoint_1, endpoint_2 = _path_endpoints(data.path_points)
    for label, endpoint in (("Endpoint 1", endpoint_1), ("Endpoint 2", endpoint_2)):
        if endpoint is None:
            continue
        note = f"step {endpoint.label}"
        if ts is not None:
            note += f", from TS {endpoint.relative_kcal - ts.relative_kcal:+.2f} kcal/mol"
        cards.append(
            metric_card(label, f"{endpoint.relative_kcal:+.2f} <small>kcal/mol</small>", note)
        )
    if include_common and data.result is not None and data.result.energy_hartree is not None:
        level = "/".join(part for part in (data.method, data.basis_set) if part)
        cards.append(
            metric_card(
                "Final energy", f"{data.result.energy_hartree:.6f} <small>Eh</small>", level
            )
        )
    if include_optimization and data.optimization_steps:
        cards.append(
            metric_card(
                "TS opt cycles" if _is_ts_route(data.route_line) else "Opt cycles",
                str(data.optimization_steps[-1][0]),
                "converged" if data.optimization_converged else "not converged",
            )
        )
    if include_vibrational and data.imaginary_count is not None:
        expected = 1 if _is_ts_route(data.route_line) else 0
        cards.append(
            metric_card(
                "Imaginary frequencies",
                str(data.imaginary_count),
                f"expected {expected}",
            )
        )
    if include_attempts:
        cards.append(
            metric_card(
                "Attempts",
                str(len(data.attempts)),
                data.total_duration_text and f"total wall time {data.total_duration_text}",
            )
        )
    return "".join(cards)


def _path_table_html(points: Sequence[IrcPathPoint]) -> str:
    if not points:
        return ""
    rows = []
    for point in points:
        marker = f"<= {html.escape(point.marker)}" if point.marker else ""
        rows.append(
            "<tr>"
            f"<td>{html.escape(point.label)}</td>"
            f"<td>{point.energy_hartree:.6f}</td>"
            f"<td>{point.relative_kcal:+.2f}</td>"
            f"<td>{point.max_gradient:.5f}</td>"
            f"<td>{point.rms_gradient:.5f}</td>"
            f"<td>{marker}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Step</th><th>E / Eh</th>"
        "<th>ΔE / kcal·mol⁻¹</th><th>max(|G|)</th><th>RMS(G)</th>"
        f"<th>Marker</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _iterations_table_html(points: Sequence[IrcIterationPoint]) -> str:
    if not points:
        return ""
    rows = [
        "<tr>"
        f"<td>{html.escape(point.direction)}</td>"
        f"<td>{point.iteration}</td>"
        f"<td>{point.energy_hartree:.6f}</td>"
        f"<td>{point.delta_e_kcal:+.2f}</td>"
        f"<td>{point.max_gradient:.5f}</td>"
        f"<td>{point.rms_gradient:.5f}</td>"
        "</tr>"
        for point in points
    ]
    return (
        "<table><thead><tr><th>Direction</th><th>Iteration</th>"
        "<th>E / Eh</th><th>ΔE / kcal·mol⁻¹</th><th>max(|G|)</th><th>RMS(G)</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


__all__ = [
    "IrcIterationPoint",
    "IrcParsedOutput",
    "IrcPathPoint",
    "IrcReportData",
    "IrcReportError",
    "IrcSiBlock",
    "collect_irc_report_data",
    "collect_irc_si_block",
    "input_uses_irc",
    "irc_report_badges",
    "irc_report_component",
    "irc_report_meta_html",
    "parse_irc_output",
    "render_irc_si_block_md",
]
