"""NEB-TS job report: CI-NEB path profile, TS refinement, frequencies."""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..evidence import (
    final_out_name,
    parsed_frequency_analysis,
    parsed_optimization_progress,
    parsed_output_facts,
)
from ..frequencies import (
    ModeSummary,
    find_frequency_analysis,
    mode_summaries,
)
from ..input_blocks import file_route_lines
from ..orca_opt_progress import OptProgress
from ..parser import KCAL_PER_HARTREE
from ..parser.extractors import parse_optimization_cycles
from .attempts import (
    AttemptReportRow,
    attempt_dicts,
    attempt_report_rows,
    attempts_metric_card,
    attempts_table_html,
    duration_text,
    latest_attempt_with_content,
    parse_attempt_output,
    terminal_actions_html,
    with_details,
)
from .frequencies import (
    mode_section_html,
)
from .path import (
    NebPathPoint,
    PathPoint,
    iter_phase_table_rows,
    parse_path_summary,
    path_marker_index,
    path_profile_chart_svg,
    path_summary_row_re,
    path_table_html,
)
from .render import (
    ChartSeries,
    ReportComponent,
    job_meta_html,
    line_chart_svg,
    metric_card,
    path_marker_point,
    relative_energy_cycle_chart_svg,
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
_PATH_SUMMARY_ROW_RE = path_summary_row_re(r"TS|\d+")
_POSSIBLE_INTERMEDIATE_RE = re.compile(
    r"Possible intermediate minimum found at image\(s\):\s*([^\n]+)", re.IGNORECASE
)
_NEB_CONVERGED_RE = re.compile(r"THE\s+NEB\s+OPTIMIZATION\s+HAS\s+CONVERGED", re.I)
_TS_CONVERGED_RE = re.compile(r"THE\s+TS\s+OPTIMIZATION\s+HAS\s+CONVERGED", re.I)


@dataclass(frozen=True)
class NebIterationPoint:
    phase: str
    iteration: int
    image: int
    delta_e_hartree: float
    max_force: float
    rms_force: float
    ci_max_force: float | None
    ci_rms_force: float | None


@dataclass(frozen=True)
class NebParsedOutput:
    settings: tuple[ReportSetting, ...]
    iterations: tuple[NebIterationPoint, ...]
    path_points: tuple[NebPathPoint, ...]
    possible_intermediates: tuple[str, ...]
    neb_converged: bool
    ts_converged: bool
    ts_steps: tuple[tuple[int, float], ...] = ()


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


def parse_neb_output_text(text: str) -> NebParsedOutput:
    """NEB facts of decoded output text; ``parse_neb_output`` memoizes this per file."""
    return NebParsedOutput(
        settings=_parse_neb_settings(text),
        iterations=_parse_neb_iterations(text),
        path_points=_parse_path_summary(text),
        possible_intermediates=tuple(
            item.strip() for item in _POSSIBLE_INTERMEDIATE_RE.findall(text) if item.strip()
        ),
        neb_converged=bool(_NEB_CONVERGED_RE.search(text)),
        ts_converged=bool(_TS_CONVERGED_RE.search(text)),
        ts_steps=_parse_ts_refinement_steps(text),
    )


def parse_neb_output(out_path: Path) -> NebParsedOutput:
    """Read-only NEB facts from the shared per-file evidence snapshot."""
    return parsed_output_facts(out_path, parse_neb_output_text)


def _neb_ts_steps(out_path: Path) -> tuple[tuple[int, float], ...]:
    return parse_neb_output(out_path).ts_steps


def _has_opt_steps(progress: OptProgress) -> bool:
    return bool(progress.steps)


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
    parsed_attempts = [parse_attempt_output(attempt, parse_neb_output) for attempt in attempts]
    rows = with_details(
        attempt_report_rows(attempts, "initial NEB-TS"),
        [_attempt_detail(parsed) for parsed in parsed_attempts],
    )

    # Prefer the latest attempt whose output actually contains NEB data (or
    # optimization cycles); an execution that died before the driver started parses
    # to an empty shell and must not mask an earlier attempt's results.
    parsed = (
        latest_attempt_with_content(attempts, parse_neb_output, _neb_parse_has_content)
        or _EMPTY_NEB_OUTPUT
    )
    progress = latest_attempt_with_content(attempts, parsed_optimization_progress, _has_opt_steps)
    ts_steps = latest_attempt_with_content(attempts, _neb_ts_steps, bool) or ()
    formula = method = basis_set = ""
    final_energy = _ts_path_energy(parsed.path_points)
    opt_converged = False
    if progress is not None:
        formula, method, basis_set = progress.formula, progress.method, progress.basis_set
        opt_converged = bool(progress.is_converged)
        if final_energy is None and ts_steps:
            final_energy = ts_steps[-1][1]

    analysis, frequency_attempt_index = find_frequency_analysis(
        attempts, parse_analysis_fn=parsed_frequency_analysis
    )

    final_result = state.get("final_result")
    final_payload: Mapping[str, Any] = final_result if isinstance(final_result, Mapping) else {}

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
        attempts=rows,
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
        last_out_name=final_out_name(state),
    )


def _neb_parse_has_content(parsed: NebParsedOutput) -> bool:
    return bool(parsed.path_points or parsed.iterations or parsed.settings)


_EMPTY_NEB_OUTPUT = NebParsedOutput(
    settings=(),
    iterations=(),
    path_points=(),
    possible_intermediates=(),
    neb_converged=False,
    ts_converged=False,
)


def _attempt_detail(parsed: NebParsedOutput | None) -> str:
    if parsed is None:
        return ""
    parts = []
    if parsed.path_points:
        parts.append(f"{len(parsed.path_points)} path pts")
    if parsed.iterations:
        parts.append(f"{parsed.iterations[-1].iteration} NEB iter")
    if parsed.ts_steps:
        parts.append(f"{len(parsed.ts_steps)} TS cycles")
    return ", ".join(parts)


def _parse_ts_refinement_steps(text: str) -> tuple[tuple[int, float], ...]:
    neb_markers = list(_NEB_CONVERGED_RE.finditer(text))
    if neb_markers:
        text = text[neb_markers[-1].end() :]

    return tuple(
        (cycle_num, energy)
        for cycle_num, energy, _ in parse_optimization_cycles(text)
        if energy is not None
    )


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


def _neb_phase_of_line(line: str) -> str | None:
    if _HEI_HEADER_RE.search(line):
        return "HEI"
    if _CI_HEADER_RE.search(line):
        return "CI"
    return None


def _parse_neb_iterations(text: str) -> tuple[NebIterationPoint, ...]:
    return tuple(
        NebIterationPoint(
            phase=phase,
            iteration=int(match.group(2)),
            image=int(match.group(3)),
            delta_e_hartree=float(match.group(4)),
            max_force=float(match.group(5)),
            rms_force=float(match.group(6)),
            ci_max_force=_optional_float(match.group(8)),
            ci_rms_force=_optional_float(match.group(9)),
        )
        for phase, match in iter_phase_table_rows(
            text, phase_of_line=_neb_phase_of_line, row_re=_NEB_ITERATION_RE
        )
    )


def _parse_path_summary(text: str) -> tuple[NebPathPoint, ...]:
    return parse_path_summary(
        text,
        header_re=_PATH_SUMMARY_HEADER_RE,
        row_re=_PATH_SUMMARY_ROW_RE,
        point_type=NebPathPoint,
    )


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


def _path_chart_svg(data: NebReportData) -> str:
    points = data.path_points
    return path_profile_chart_svg(
        points,
        x_of=lambda index: _path_plot_x(points, index),
        path_label="NEB path",
        highlights=(
            ("climbing image", "#d97706", path_marker_index(points, "CI")),
            ("optimized TS", "#158a72", path_marker_index(points, "TS")),
        ),
        x_label="path image index",
        y_label="dE / kcal mol⁻¹",
        x_ticks=_path_x_ticks(points),
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
    ci = path_marker_point(data.path_points, "CI")
    ts = path_marker_point(data.path_points, "TS")
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
        cards.append(attempts_metric_card(data.attempts, data.total_duration_text))
    return "".join(cards)


def _path_profile_html(data: NebReportData) -> str:
    chart = _path_chart_svg(data) or (
        '<p class="muted">No NEB path-summary points were parsed from the attempt outputs.</p>'
    )
    table = path_table_html(data.path_points, _PATH_TABLE_COLUMNS)
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


_PATH_TABLE_COLUMNS: tuple[tuple[str, Callable[[PathPoint], str]], ...] = (
    ("Image", lambda point: html.escape(point.label)),
    ("dE kcal/mol", lambda point: f"{point.relative_kcal:.2f}"),
    ("E(Eh)", lambda point: f"{point.energy_hartree:.6f}"),
    ("max(|Fp|)", lambda point: f"{point.max_gradient:.5f}"),
    ("RMS(Fp)", lambda point: f"{point.rms_gradient:.5f}"),
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
    ts_chart = relative_energy_cycle_chart_svg(data.ts_steps, x_label="TS optimization cycle") or (
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
    "parse_neb_output",
    "parse_neb_output_text",
    "neb_report_component",
    "neb_report_meta_html",
]
