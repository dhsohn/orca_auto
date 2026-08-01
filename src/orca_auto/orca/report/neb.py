"""NEB-TS job report: CI-NEB path profile, TS refinement, frequencies."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.orca.parser.patterns import (
    FINAL_SINGLE_POINT_ENERGY_RE,
    final_single_point_energy_value,
)

from ..input_blocks import file_route_lines
from ..orca_opt_progress import OptProgress, parse_opt_progress
from ..parser import KCAL_PER_HARTREE
from ..parser.io import read_orca_text
from .attempts import (
    AttemptReportRow,
    analyzer_status_text,
    attempt_actions,
    attempt_dicts,
    attempt_role,
    attempts_table_html,
    duration_text,
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

_NEB_TS_ROUTE_RE = re.compile(r"\b(?:ZOOM-)?NEB-TS\b", re.IGNORECASE)
_NEB_SETTINGS_HEADER_RE = re.compile(r"^\s*NEB settings\s*$", re.IGNORECASE)
_HEI_HEADER_RE = re.compile(r"\bE\(HEI\)-E\(0\)", re.IGNORECASE)
_CI_HEADER_RE = re.compile(r"\bE\(CI\)-E\(0\)", re.IGNORECASE)
_NEB_ITERATION_RE = re.compile(
    r"^\s*([A-Za-z][\w-]*)\s+"
    r"(\d+)\s+"
    r"(\d+)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)"
    r"(?:\s+([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?))?"
    r"(?:\s+([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?))?\s*$"
)
_PATH_SUMMARY_HEADER_RE = re.compile(r"\bPATH SUMMARY FOR\s+(?:ZOOM-)?NEB(?:-TS|-CI)?\b", re.I)
_PATH_SUMMARY_ROW_RE = re.compile(
    r"^\s*(TS|\d+)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)"
    r"(?:\s+<=\s*([A-Za-z-]+))?\s*$",
    re.IGNORECASE,
)
_POSSIBLE_INTERMEDIATE_RE = re.compile(
    r"Possible intermediate minimum found at image\(s\):\s*([^\n]+)", re.IGNORECASE
)
_NEB_CONVERGED_RE = re.compile(r"THE\s+NEB\s+OPTIMIZATION\s+HAS\s+CONVERGED", re.I)
_TS_CONVERGED_RE = re.compile(r"THE\s+TS\s+OPTIMIZATION\s+HAS\s+CONVERGED", re.I)
_GEOM_CYCLE_RE = re.compile(r"Geometry Optimization Cycle\s+(\d+)", re.I)


@dataclass(frozen=True)
class NebIterationPoint:
    phase: str
    optimizer: str
    iteration: int
    image: int
    delta_e_hartree: float
    max_force: float
    rms_force: float
    path_length: float
    ci_max_force: float | None
    ci_rms_force: float | None


@dataclass(frozen=True)
class NebPathPoint:
    label: str
    order: int
    image_index: int | None
    energy_hartree: float
    relative_kcal: float
    max_force: float
    rms_force: float
    marker: str


@dataclass(frozen=True)
class NebParsedOutput:
    settings: tuple[ReportSetting, ...]
    iterations: tuple[NebIterationPoint, ...]
    path_points: tuple[NebPathPoint, ...]
    possible_intermediates: tuple[str, ...]
    neb_converged: bool
    ts_converged: bool


@dataclass(frozen=True)
class NebReportData:
    title: str
    job_id: str
    status: str
    reason: str
    route_line: str
    formula: str
    method: str
    basis_set: str
    started_at: str
    finished_at: str
    total_duration_text: str
    attempts: tuple[AttemptReportRow, ...]
    settings: tuple[ReportSetting, ...]
    iterations: tuple[NebIterationPoint, ...]
    path_points: tuple[NebPathPoint, ...]
    possible_intermediates: tuple[str, ...]
    neb_converged: bool
    ts_converged: bool
    ts_steps: tuple[tuple[int, float], ...]
    final_energy: float | None
    imaginary_count: int | None
    mode_summaries: tuple[ModeSummary, ...]
    frequency_attempt_index: int | None
    last_out_name: str


def input_uses_neb_ts(inp_path: Path) -> bool:
    return bool(_NEB_TS_ROUTE_RE.search(" ".join(file_route_lines(inp_path))))


def parse_neb_output(out_path: Path) -> NebParsedOutput:
    text = read_orca_text(str(out_path))
    return NebParsedOutput(
        settings=_parse_neb_settings(text),
        iterations=_parse_neb_iterations(text),
        path_points=_parse_path_summary(text),
        possible_intermediates=tuple(
            item.strip() for item in _POSSIBLE_INTERMEDIATE_RE.findall(text) if item.strip()
        ),
        neb_converged=bool(_NEB_CONVERGED_RE.search(text)),
        ts_converged=bool(_TS_CONVERGED_RE.search(text)),
    )


def collect_neb_report_data(
    reaction_dir: Path,
    state: Mapping[str, Any],
) -> NebReportData | None:
    selected_raw = str(state.get("selected_inp") or "").strip()
    if not selected_raw:
        return None
    selected_inp = Path(selected_raw)
    if not input_uses_neb_ts(selected_inp):
        return None

    route_lines = file_route_lines(selected_inp)
    attempts = attempt_dicts(state)
    rows: list[AttemptReportRow] = []
    parsed_attempts: list[NebParsedOutput] = []
    progress_attempts: list[OptProgress] = []
    ts_steps_attempts: list[tuple[tuple[int, float], ...]] = []

    for position, attempt in enumerate(attempts):
        if position == 0:
            label = "initial NEB-TS"
        else:
            label, _direction = attempt_role(attempt_actions(attempts[position - 1]))
        index = int(attempt.get("index", position + 1) or (position + 1))
        parsed = _parse_attempt_neb_output(attempt)
        progress = _parse_attempt_opt_progress(attempt)
        ts_steps = _parse_attempt_ts_steps(attempt)
        if parsed is not None:
            parsed_attempts.append(parsed)
        if progress is not None:
            progress_attempts.append(progress)
        if ts_steps:
            ts_steps_attempts.append(ts_steps)
        rows.append(
            AttemptReportRow(
                index=index,
                label=label,
                direction="forward",
                analyzer_status=analyzer_status_text(attempt.get("analyzer_status")),
                analyzer_reason=str(attempt.get("analyzer_reason") or ""),
                duration_text=duration_text(attempt.get("started_at"), attempt.get("ended_at")),
                detail=_attempt_detail(parsed, ts_steps),
                terminal_actions=attempt_actions(attempt) if position == len(attempts) - 1 else (),
            )
        )

    # Prefer the latest attempt whose output actually contains NEB data (or
    # optimization cycles); a retry that died before the driver started parses
    # to an empty shell and must not mask an earlier attempt's results.
    parsed = next(
        (entry for entry in reversed(parsed_attempts) if _neb_parse_has_content(entry)),
        parsed_attempts[-1] if parsed_attempts else _empty_neb_parsed_output(),
    )
    progress = next(
        (entry for entry in reversed(progress_attempts) if entry.steps),
        progress_attempts[-1] if progress_attempts else None,
    )
    ts_steps = ts_steps_attempts[-1] if ts_steps_attempts else ()
    formula = method = basis_set = ""
    final_energy = _ts_path_energy(parsed.path_points)
    opt_converged = False
    if progress is not None:
        formula, method, basis_set = progress.formula, progress.method, progress.basis_set
        opt_converged = bool(progress.is_converged)
        if final_energy is None and ts_steps:
            final_energy = ts_steps[-1][1]

    analysis, frequency_attempt_index = find_frequency_analysis(attempts)

    final_result = state.get("final_result")
    final_payload: Mapping[str, Any] = final_result if isinstance(final_result, Mapping) else {}
    last_out = str(final_payload.get("last_out_path") or "").strip()
    if not last_out and attempts:
        last_out = str(attempts[-1].get("out_path") or "").strip()

    return NebReportData(
        title=reaction_dir.name or str(reaction_dir),
        job_id=str(state.get("job_id") or ""),
        status=str(state.get("status") or ""),
        reason=str(final_payload.get("reason") or ""),
        route_line=route_lines[0] if route_lines else "",
        formula=formula,
        method=method,
        basis_set=basis_set,
        started_at=str(state.get("started_at") or ""),
        finished_at=str(final_payload.get("completed_at") or ""),
        total_duration_text=duration_text(
            state.get("started_at"), final_payload.get("completed_at")
        ),
        attempts=tuple(rows),
        settings=parsed.settings,
        iterations=parsed.iterations,
        path_points=parsed.path_points,
        possible_intermediates=parsed.possible_intermediates,
        neb_converged=parsed.neb_converged,
        ts_converged=parsed.ts_converged or opt_converged,
        ts_steps=ts_steps,
        final_energy=final_energy,
        imaginary_count=analysis.imaginary_count() if analysis is not None else None,
        mode_summaries=mode_summaries(analysis, None) if analysis is not None else (),
        frequency_attempt_index=frequency_attempt_index,
        last_out_name=Path(last_out).name if last_out else "",
    )


def _parse_attempt_neb_output(attempt: Mapping[str, Any]) -> NebParsedOutput | None:
    out_raw = str(attempt.get("out_path") or "").strip()
    if not out_raw:
        return None
    out_path = Path(out_raw)
    if not out_path.exists():
        return None
    try:
        return parse_neb_output(out_path)
    except (OSError, FileNotFoundError):
        return None


def _parse_attempt_opt_progress(attempt: Mapping[str, Any]) -> OptProgress | None:
    out_raw = str(attempt.get("out_path") or "").strip()
    if not out_raw:
        return None
    out_path = Path(out_raw)
    if not out_path.exists():
        return None
    try:
        return parse_opt_progress(str(out_path))
    except (OSError, FileNotFoundError):
        return None


def _parse_attempt_ts_steps(attempt: Mapping[str, Any]) -> tuple[tuple[int, float], ...]:
    out_raw = str(attempt.get("out_path") or "").strip()
    if not out_raw:
        return ()
    out_path = Path(out_raw)
    if not out_path.exists():
        return ()
    try:
        return _parse_ts_refinement_steps(out_path)
    except (OSError, FileNotFoundError):
        return ()


def _neb_parse_has_content(parsed: NebParsedOutput) -> bool:
    return bool(parsed.path_points or parsed.iterations or parsed.settings)


def _empty_neb_parsed_output() -> NebParsedOutput:
    return NebParsedOutput(
        settings=(),
        iterations=(),
        path_points=(),
        possible_intermediates=(),
        neb_converged=False,
        ts_converged=False,
    )


def _attempt_detail(
    parsed: NebParsedOutput | None,
    ts_steps: Sequence[tuple[int, float]],
) -> str:
    parts = []
    if parsed is not None:
        if parsed.path_points:
            parts.append(f"{len(parsed.path_points)} path pts")
        if parsed.iterations:
            parts.append(f"{parsed.iterations[-1].iteration} NEB iter")
    if ts_steps:
        parts.append(f"{len(ts_steps)} TS cycles")
    return ", ".join(parts)


def _parse_ts_refinement_steps(out_path: Path) -> tuple[tuple[int, float], ...]:
    text = read_orca_text(str(out_path))
    neb_markers = list(_NEB_CONVERGED_RE.finditer(text))
    if neb_markers:
        text = text[neb_markers[-1].end() :]

    cycle_positions = [
        (match.start(), int(match.group(1))) for match in _GEOM_CYCLE_RE.finditer(text)
    ]
    energy_positions = []
    for energy_match in FINAL_SINGLE_POINT_ENERGY_RE.finditer(text):
        try:
            energy_positions.append(
                (energy_match.start(), final_single_point_energy_value(energy_match.group(1)))
            )
        except ValueError:
            continue
    steps: list[tuple[int, float]] = []
    for position, (cycle_start, cycle_num) in enumerate(cycle_positions):
        cycle_end = (
            cycle_positions[position + 1][0] if position + 1 < len(cycle_positions) else len(text)
        )
        energy = None
        for energy_position, energy_value in energy_positions:
            if cycle_start <= energy_position < cycle_end:
                energy = energy_value
        if energy is not None:
            steps.append((cycle_num, energy))
    return tuple(steps)


def _parse_neb_settings(text: str) -> tuple[ReportSetting, ...]:
    settings: list[ReportSetting] = []
    in_settings = False
    for line in text.splitlines():
        if _NEB_SETTINGS_HEADER_RE.match(line):
            in_settings = True
            continue
        if not in_settings:
            continue
        # Match dotted rows before checking terminators: "Generation of
        # initial path ....  idpp" is itself a setting, while the section ends
        # at the non-dotted "Generation of  the initial path:" narration.
        setting = match_dotted_setting(line)
        if setting is not None:
            settings.append(setting)
            continue
        if line.strip().lower().startswith(("generation of", "starting iterations")):
            break
    return tuple(settings)


def _parse_neb_iterations(text: str) -> tuple[NebIterationPoint, ...]:
    phase = ""
    table_started = False
    points: list[NebIterationPoint] = []
    for line in text.splitlines():
        if _HEI_HEADER_RE.search(line):
            phase = "HEI"
            table_started = False
            continue
        if _CI_HEADER_RE.search(line):
            phase = "CI"
            table_started = False
            continue
        if not phase:
            continue
        match = _NEB_ITERATION_RE.match(line)
        if match is None and table_started and line.strip():
            phase = ""
            table_started = False
            continue
        if match is None:
            continue
        table_started = True
        points.append(
            NebIterationPoint(
                phase=phase,
                optimizer=match.group(1),
                iteration=int(match.group(2)),
                image=int(match.group(3)),
                delta_e_hartree=float(match.group(4)),
                max_force=float(match.group(5)),
                rms_force=float(match.group(6)),
                path_length=float(match.group(7)),
                ci_max_force=_optional_float(match.group(8)),
                ci_rms_force=_optional_float(match.group(9)),
            )
        )
    return tuple(points)


def _parse_path_summary(text: str) -> tuple[NebPathPoint, ...]:
    points: list[NebPathPoint] = []
    in_summary = False
    for line in text.splitlines():
        if _PATH_SUMMARY_HEADER_RE.search(line):
            in_summary = True
            points = []
            continue
        if not in_summary:
            continue
        match = _PATH_SUMMARY_ROW_RE.match(line)
        if match is None:
            if points and not line.strip():
                in_summary = False
            continue
        label = match.group(1).upper()
        image_index = None if label == "TS" else int(label)
        marker = (match.group(6) or "").upper()
        points.append(
            NebPathPoint(
                label=label,
                order=len(points),
                image_index=image_index,
                energy_hartree=float(match.group(2)),
                relative_kcal=float(match.group(3)),
                max_force=float(match.group(4)),
                rms_force=float(match.group(5)),
                marker=marker,
            )
        )
    return tuple(points)


def _optional_float(value: str | None) -> float | None:
    return None if value is None else float(value)


def _ts_path_energy(points: Sequence[NebPathPoint]) -> float | None:
    for point in points:
        if point.marker == "TS" or point.label == "TS":
            return point.energy_hartree
    return None


def _path_peak(points: Sequence[NebPathPoint]) -> NebPathPoint | None:
    if not points:
        return None
    return max(points, key=lambda point: point.relative_kcal)


def _path_marker(points: Sequence[NebPathPoint], marker: str) -> NebPathPoint | None:
    marker = marker.upper()
    for point in points:
        if point.marker == marker or point.label == marker:
            return point
    return None


def _path_marker_index(points: Sequence[NebPathPoint], marker: str) -> int | None:
    marker = marker.upper()
    for index, point in enumerate(points):
        if point.marker == marker or point.label == marker:
            return index
    return None


def _path_plot_x(points: Sequence[NebPathPoint], index: int) -> float:
    point = points[index]
    if point.image_index is not None:
        return float(point.image_index)

    previous_image = next(
        (
            candidate.image_index
            for candidate in reversed(points[:index])
            if candidate.image_index is not None
        ),
        None,
    )
    next_image = next(
        (
            candidate.image_index
            for candidate in points[index + 1 :]
            if candidate.image_index is not None
        ),
        None,
    )
    if previous_image is not None and next_image is not None:
        return (previous_image + next_image) / 2
    if previous_image is not None:
        return float(previous_image) + 0.5
    if next_image is not None:
        return float(next_image) - 0.5
    return float(point.order)


def _path_x_ticks(points: Sequence[NebPathPoint]) -> tuple[float, ...]:
    image_indices = sorted({point.image_index for point in points if point.image_index is not None})
    if not image_indices:
        return ()
    low, high = image_indices[0], image_indices[-1]
    step = max(1, (high - low + 9) // 10)
    ticks = list(range(low, high + 1, step))
    if ticks[-1] != high:
        ticks.append(high)
    return tuple(float(tick) for tick in ticks)


def _convergence_chart_svg(steps: Sequence[tuple[int, float]]) -> str:
    if len(steps) < 2:
        return ""
    e_min = min(energy for _cycle, energy in steps)
    points = tuple((float(cycle), (energy - e_min) * KCAL_PER_HARTREE) for cycle, energy in steps)
    return line_chart_svg(
        (ChartSeries(label="", color="#2f6fb2", dash="", points=points),),
        x_label="TS optimization cycle",
        y_label="dE / kcal mol⁻¹",
        x_tick_fmt=".0f",
    )


def _path_chart_svg(data: NebReportData) -> str:
    if len(data.path_points) < 2:
        return ""
    series = [
        ChartSeries(
            label="NEB path",
            color="#2f6fb2",
            dash="",
            points=tuple(
                (_path_plot_x(data.path_points, index), point.relative_kcal)
                for index, point in enumerate(data.path_points)
            ),
        )
    ]
    ci_index = _path_marker_index(data.path_points, "CI")
    ts_index = _path_marker_index(data.path_points, "TS")
    if ci_index is not None:
        ci = data.path_points[ci_index]
        series.append(
            ChartSeries(
                label="climbing image",
                color="#d97706",
                dash="",
                points=((_path_plot_x(data.path_points, ci_index), ci.relative_kcal),),
            )
        )
    if ts_index is not None:
        ts = data.path_points[ts_index]
        series.append(
            ChartSeries(
                label="optimized TS",
                color="#158a72",
                dash="",
                points=((_path_plot_x(data.path_points, ts_index), ts.relative_kcal),),
            )
        )
    return line_chart_svg(
        tuple(series),
        x_label="path image index",
        y_label="dE / kcal mol⁻¹",
        x_tick_fmt=".0f",
        x_ticks=_path_x_ticks(data.path_points),
    )


def _neb_history_chart_svg(data: NebReportData) -> str:
    if len(data.iterations) < 2:
        return ""
    series = []
    for phase, color, dash in (("HEI", "#2f6fb2", ""), ("CI", "#d97706", "6 4")):
        phase_points = tuple(
            (float(point.iteration), point.delta_e_hartree * KCAL_PER_HARTREE)
            for point in data.iterations
            if point.phase == phase
        )
        if phase_points:
            series.append(
                ChartSeries(
                    label=f"{phase} image",
                    color=color,
                    dash=dash,
                    points=phase_points,
                )
            )
    return line_chart_svg(
        tuple(series),
        x_label="NEB iteration",
        y_label="dE(image)-E(0) / kcal mol⁻¹",
        x_tick_fmt=".0f",
    )


def neb_report_badges(data: NebReportData) -> tuple[tuple[str, str], ...]:
    badges = status_badges(data.status, data.reason)
    if data.neb_converged:
        badges.append(("CI-NEB converged", "ok"))
    if data.ts_converged:
        badges.append(("TS optimized", "ok"))
    return tuple(badges)


def neb_report_meta_html(data: NebReportData) -> str:
    formula_text = f" &#183; {html.escape(data.formula)}" if data.formula else ""
    return job_meta_html(
        route_line=data.route_line,
        job_id=data.job_id,
        started_at=data.started_at,
        finished_at=data.finished_at,
        extra_html=formula_text,
    )


def _metric_cards(
    data: NebReportData,
    *,
    include_attempts: bool = True,
    include_frequency: bool = True,
) -> str:
    cards = []
    peak = _path_peak(data.path_points)
    ci = _path_marker(data.path_points, "CI")
    ts = _path_marker(data.path_points, "TS")
    if peak is not None:
        peak_label = f"image {peak.label}"
        if peak.marker:
            peak_label += f" ({peak.marker})"
        cards.append(
            metric_card(
                "NEB path barrier",
                f"{peak.relative_kcal:.2f} <small>kcal/mol</small>",
                peak_label,
            )
        )
    if ci is not None and ts is not None:
        cards.append(
            metric_card(
                "CI to TS shift",
                f"{ts.relative_kcal - ci.relative_kcal:+.2f} <small>kcal/mol</small>",
                "optimized TS relative to climbing image",
            )
        )
    if data.iterations:
        last = data.iterations[-1]
        cards.append(
            metric_card(
                "NEB iterations",
                str(last.iteration),
                f"{last.phase} image {last.image}",
            )
        )
    if data.final_energy is not None:
        cards.append(
            metric_card(
                "Final TS energy",
                f"{data.final_energy:.6f} <small>Eh</small>",
                f"{data.method}/{data.basis_set}" if data.method and data.basis_set else "",
            )
        )
    if include_frequency and data.imaginary_count is not None:
        cards.append(
            metric_card(
                "Imaginary frequencies",
                str(data.imaginary_count),
                f"from attempt {data.frequency_attempt_index}"
                if data.frequency_attempt_index is not None
                else "",
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


def _path_profile_html(data: NebReportData) -> str:
    chart = _path_chart_svg(data) or (
        '<p class="muted">No NEB path-summary points were parsed from the attempt outputs.</p>'
    )
    table = _path_table_html(data.path_points)
    notes = []
    if data.possible_intermediates:
        notes.append(
            "Possible intermediate minimum image(s): "
            f"<code>{html.escape(', '.join(data.possible_intermediates))}</code>"
        )
    if data.neb_converged:
        notes.append("CI-NEB converged before the final TS optimization.")
    note_html = "".join(f'<p class="muted">{note}</p>' for note in notes)
    return chart + table + note_html


def _path_table_html(points: Sequence[NebPathPoint]) -> str:
    if not points:
        return ""
    rows = []
    for point in points:
        marker = f"<= {html.escape(point.marker)}" if point.marker else ""
        rows.append(
            "<tr>"
            f"<td>{html.escape(point.label)}</td>"
            f"<td>{point.relative_kcal:.2f}</td>"
            f"<td>{point.energy_hartree:.6f}</td>"
            f"<td>{point.max_force:.5f}</td>"
            f"<td>{point.rms_force:.5f}</td>"
            f"<td>{marker}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Image</th><th>dE kcal/mol</th><th>E(Eh)</th>"
        "<th>max(|Fp|)</th><th>RMS(Fp)</th><th>Marker</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _neb_history_html(data: NebReportData) -> str:
    chart = _neb_history_chart_svg(data) or (
        '<p class="muted">No NEB-CI iteration table was parsed from the attempt outputs.</p>'
    )
    if not data.iterations:
        return chart
    last = data.iterations[-1]
    extra = ""
    if last.ci_max_force is not None and last.ci_rms_force is not None:
        extra = f"; CI force max {last.ci_max_force:.5f}, RMS {last.ci_rms_force:.5f}"
    return (
        chart
        + '<p class="muted">'
        + (
            f"Last parsed {html.escape(last.phase)} step: iteration {last.iteration}, "
            f"image {last.image}, dE {last.delta_e_hartree * KCAL_PER_HARTREE:.2f} "
            f"kcal/mol, max force {last.max_force:.5f}, RMS force {last.rms_force:.5f}"
            f"{extra}."
        )
        + "</p>"
    )


def neb_report_component(
    data: NebReportData,
    *,
    include_attempt_metric: bool = True,
    include_attempt_chain: bool = True,
    include_frequency_metric: bool = True,
    include_vibrational: bool = True,
) -> ReportComponent:
    ts_chart = _convergence_chart_svg(data.ts_steps) or (
        '<p class="muted">No TS optimization cycles were parsed from the attempt outputs.</p>'
    )
    sections: list[tuple[str, str]] = [
        ("NEB-CI path profile", _path_profile_html(data)),
        ("NEB-CI optimization", _neb_history_html(data)),
        ("TS optimization convergence", ts_chart),
    ]
    settings_html = settings_table_html(data.settings)
    if settings_html:
        sections.append(("NEB setup", settings_html))
    if include_attempt_chain:
        attempts_html = attempts_table_html(data.attempts, "NEB/TS detail") + terminal_actions_html(
            data.attempts
        )
        sections.append(("Attempt chain", attempts_html))
    if include_vibrational:
        sections.append(
            (
                "Vibrational summary",
                mode_section_html(
                    data.mode_summaries,
                    None,
                    frequency_calculation_found=data.frequency_attempt_index is not None,
                ),
            )
        )

    return ReportComponent(
        metrics_html=_metric_cards(
            data,
            include_attempts=include_attempt_metric,
            include_frequency=include_frequency_metric,
        ),
        sections=tuple(sections),
    )


__all__ = [
    "NebIterationPoint",
    "NebParsedOutput",
    "NebPathPoint",
    "NebReportData",
    "collect_neb_report_data",
    "input_uses_neb_ts",
    "neb_report_badges",
    "neb_report_component",
    "neb_report_meta_html",
    "parse_neb_output",
]
