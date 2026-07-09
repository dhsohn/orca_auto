"""ScanTS job report: scan energy profile, retry chain, vibrational summary."""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..input_blocks import file_route_lines
from ..scants import (
    SCANTS_BARRIER_NOISE_KCAL,
    ScanCoordinateSpec,
    ScanTSSurfacePoint,
    first_scan_coordinate_spec,
    input_uses_scants,
    parse_scants_actual_surface,
    scan_profile_interior_barrier_kcal,
)
from .attempts import (
    AttemptReportRow,
    attempt_actions,
    attempt_dicts,
    attempt_role,
    attempts_table_html,
    duration_text,
    terminal_actions_html,
)
from .frequencies import ModeSummary, find_frequency_analysis, mode_section_html, mode_summaries
from .render import (
    KCAL_PER_HARTREE,
    ChartSeries,
    ReportComponent,
    ReportPage,
    job_meta_html,
    line_chart_svg,
    metric_card,
    render_page,
    status_badges,
    verdict_note,
)


@dataclass(frozen=True)
class ScanSegment:
    attempt_index: int
    role: str
    points: tuple[ScanTSSurfacePoint, ...]


@dataclass(frozen=True)
class ScantsReportData:
    title: str
    kind: str
    job_id: str
    status: str
    reason: str
    route_line: str
    scan_spec: ScanCoordinateSpec | None
    started_at: str
    finished_at: str
    total_duration_text: str
    attempts: tuple[AttemptReportRow, ...]
    segments: tuple[ScanSegment, ...]
    forward_barrier_kcal: float | None
    forward_drop_kcal: float | None
    imaginary_count: int | None
    mode_summaries: tuple[ModeSummary, ...]
    frequency_attempt_index: int | None
    last_out_name: str

    def kind_label(self) -> str:
        return "ScanTS" if self.kind == "scants" else "Relaxed scan"


def collect_scants_report_data(
    reaction_dir: Path,
    state: Mapping[str, Any],
    *,
    kind: str = "scants",
) -> ScantsReportData | None:
    """Report data for ScanTS (``kind="scants"``) and plain relaxed scans.

    A relaxed scan (``! Opt`` route with a ``%geom Scan`` block) prints the
    same actual-energy surface table, so it shares the profile chart, barrier
    prominence, and vibrational summary; only the TS-hunt wording differs.
    """
    selected_raw = str(state.get("selected_inp") or "").strip()
    if not selected_raw:
        return None
    selected_inp = Path(selected_raw)
    if kind == "scants" and not input_uses_scants(selected_inp):
        return None

    route_lines = file_route_lines(selected_inp)
    scan_spec = first_scan_coordinate_spec(selected_inp)
    attempts = attempt_dicts(state)

    initial_label = "initial ScanTS" if kind == "scants" else "initial relaxed scan"
    rows: list[AttemptReportRow] = []
    segments: list[ScanSegment] = []
    forward_energies: list[float] = []
    for position, attempt in enumerate(attempts):
        if position == 0:
            label, direction = initial_label, "forward"
        else:
            label, direction = attempt_role(attempt_actions(attempts[position - 1]))
        out_raw = str(attempt.get("out_path") or "").strip()
        points: tuple[ScanTSSurfacePoint, ...] = ()
        if out_raw:
            points = tuple(parse_scants_actual_surface(Path(out_raw)))
        index = int(attempt.get("index", position + 1) or (position + 1))
        rows.append(
            AttemptReportRow(
                index=index,
                label=label,
                direction=direction,
                analyzer_status=str(attempt.get("analyzer_status") or ""),
                analyzer_reason=str(attempt.get("analyzer_reason") or ""),
                duration_text=duration_text(attempt.get("started_at"), attempt.get("ended_at")),
                detail=str(len(points)) if points else "",
                terminal_actions=attempt_actions(attempt) if position == len(attempts) - 1 else (),
            )
        )
        if points:
            segments.append(ScanSegment(attempt_index=index, role=label, points=points))
            if direction == "forward":
                forward_energies.extend(point.energy for point in points)

    forward_barrier = scan_profile_interior_barrier_kcal(forward_energies)
    forward_drop = (
        (forward_energies[-1] - forward_energies[0]) * KCAL_PER_HARTREE
        if len(forward_energies) >= 2
        else None
    )

    analysis, frequency_attempt_index = find_frequency_analysis(attempts)
    alignment_pair: tuple[int, int] | None = None
    if scan_spec is not None and scan_spec.kind == "B" and len(scan_spec.atoms) == 2:
        alignment_pair = (scan_spec.atoms[0], scan_spec.atoms[1])

    final_result = state.get("final_result")
    final_payload: Mapping[str, Any] = final_result if isinstance(final_result, Mapping) else {}
    last_out = str(final_payload.get("last_out_path") or "").strip()
    if not last_out and attempts:
        last_out = str(attempts[-1].get("out_path") or "").strip()

    return ScantsReportData(
        title=reaction_dir.name or str(reaction_dir),
        kind=kind,
        job_id=str(state.get("job_id") or ""),
        status=str(state.get("status") or ""),
        reason=str(final_payload.get("reason") or ""),
        route_line=route_lines[0] if route_lines else "",
        scan_spec=scan_spec,
        started_at=str(state.get("started_at") or ""),
        finished_at=str(final_payload.get("completed_at") or ""),
        total_duration_text=duration_text(
            state.get("started_at"), final_payload.get("completed_at")
        ),
        attempts=tuple(rows),
        segments=tuple(segments),
        forward_barrier_kcal=forward_barrier,
        forward_drop_kcal=forward_drop,
        imaginary_count=analysis.imaginary_count() if analysis is not None else None,
        mode_summaries=mode_summaries(analysis, alignment_pair) if analysis is not None else (),
        frequency_attempt_index=frequency_attempt_index,
        last_out_name=Path(last_out).name if last_out else "",
    )


_SEGMENT_STYLES = {
    "initial ScanTS": ("#2f6fb2", ""),
    "initial relaxed scan": ("#2f6fb2", ""),
    "scan continuation": ("#2f6fb2", ""),
    "retry": ("#2f6fb2", ""),
    "resume": ("#2f6fb2", ""),
    "endpoint completion (Opt scan)": ("#158a72", ""),
    "reverse ScanTS": ("#d97706", "6 4"),
}


def _profile_chart_svg(data: ScantsReportData) -> str:
    all_energy = [point.energy for segment in data.segments for point in segment.points]
    if len(all_energy) < 2:
        return ""
    e_min = min(all_energy)

    series: list[ChartSeries] = []
    seen_roles: set[str] = set()
    for segment in data.segments:
        color, dash = _SEGMENT_STYLES.get(segment.role, ("#2f6fb2", ""))
        points = tuple(
            (point.coordinates[0], (point.energy - e_min) * KCAL_PER_HARTREE)
            for point in segment.points
            if point.coordinates
        )
        if not points:
            continue
        label = segment.role if segment.role not in seen_roles else ""
        seen_roles.add(segment.role)
        series.append(ChartSeries(label=label, color=color, dash=dash, points=points))

    x_label = data.scan_spec.label() if data.scan_spec is not None else "scan coordinate"
    return line_chart_svg(
        tuple(series),
        x_label=f"{x_label} / Å",
        y_label="ΔE / kcal mol⁻¹",
    )


def scants_report_badges(data: ScantsReportData) -> tuple[tuple[str, str], ...]:
    return tuple(status_badges(data.status, data.reason))


def scants_report_meta_html(data: ScantsReportData) -> str:
    scan_text = ""
    if data.scan_spec is not None:
        scan_text = (
            f" &#183; {html.escape(data.scan_spec.label())} = "
            f"{data.scan_spec.start:g} &#8594; {data.scan_spec.end:g} &#8491;, "
            f"{data.scan_spec.points} pt"
        )
    return job_meta_html(
        route_line=data.route_line,
        job_id=data.job_id,
        started_at=data.started_at,
        finished_at=data.finished_at,
        extra_html=scan_text,
    )


def _metric_cards(
    data: ScantsReportData,
    *,
    include_attempts: bool = True,
    include_frequency: bool = True,
) -> str:
    scants = data.kind == "scants"
    cards = []
    if data.forward_barrier_kcal is not None:
        below = data.forward_barrier_kcal < SCANTS_BARRIER_NOISE_KCAL
        if scants:
            barrier_note = (
                f"below {SCANTS_BARRIER_NOISE_KCAL} noise threshold"
                if below
                else f"above {SCANTS_BARRIER_NOISE_KCAL} noise threshold"
            )
        else:
            barrier_note = "prominence over the shallower flank"
        cards.append(
            metric_card(
                "Forward interior barrier" if scants else "Interior barrier",
                f"{data.forward_barrier_kcal:.2f} <small>kcal/mol</small>",
                barrier_note,
            )
        )
    if data.forward_drop_kcal is not None:
        cards.append(
            metric_card(
                "Forward profile span" if scants else "Profile span",
                f"{data.forward_drop_kcal:+.1f} <small>kcal/mol</small>",
                "endpoint relative to scan start",
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


def scants_report_component(
    data: ScantsReportData,
    *,
    include_attempt_metric: bool = True,
    include_attempt_chain: bool = True,
    include_frequency_metric: bool = True,
    include_vibrational: bool = True,
) -> ReportComponent:
    chart = _profile_chart_svg(data) or (
        '<p class="muted">No relaxed-surface points were parsed from the attempt outputs.</p>'
    )
    alignment_label = data.scan_spec.label() if data.scan_spec is not None else None
    sections: list[tuple[str, str]] = [("Scan energy profile", chart)]
    if include_attempt_chain:
        attempts_html = attempts_table_html(data.attempts, "Scan points") + terminal_actions_html(
            data.attempts
        )
        sections.append(("Attempt chain", attempts_html))
    if include_vibrational:
        sections.append(
            ("Vibrational summary", mode_section_html(data.mode_summaries, alignment_label))
        )
    return ReportComponent(
        metrics_html=_metric_cards(
            data,
            include_attempts=include_attempt_metric,
            include_frequency=include_frequency_metric,
        ),
        sections=tuple(sections),
    )


def render_scants_report_html(data: ScantsReportData) -> str:
    fallback_reason = data.attempts[-1].analyzer_reason if data.attempts else ""
    component = scants_report_component(data)

    page = ReportPage(
        title=f"{data.title} · {data.kind_label()} report",
        badges=scants_report_badges(data),
        meta_html=scants_report_meta_html(data),
        verdict_html=verdict_note(data.reason, fallback_reason),
        metrics_html=component.metrics_html,
        sections=component.sections,
        footer_html=(
            "Generated by orca_auto &#183; last output: "
            f"<code>{html.escape(data.last_out_name)}</code>"
        ),
    )
    return render_page(page)
