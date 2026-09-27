"""Relaxed-scan energy profile, attempt history, and vibrational summary."""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..completion_rules import RouteFacts
from ..frequencies import ModeSummary, mode_summaries
from ..parser import KCAL_PER_HARTREE
from ..relaxed_scan import (
    ScanCoordinateSpec,
    ScanSurfacePoint,
    first_scan_coordinate_spec,
    parse_scan_actual_surface,
    scan_profile_interior_barrier_kcal,
)
from .attempts import (
    AttemptReportRow,
    attempt_dicts,
    attempt_report_rows,
    attempts_metric_card,
    attempts_table_html,
    latest_frequency_analysis,
    parse_attempt_output,
    terminal_actions_html,
    with_details,
)
from .frequencies import mode_section_html
from .render import (
    ChartSeries,
    ReportComponent,
    ReportHeader,
    job_meta_html,
    line_chart_svg,
    metric_card,
    status_badges,
)


@dataclass(frozen=True)
class ScanSegment:
    attempt_index: int
    role: str
    points: tuple[ScanSurfacePoint, ...]


@dataclass(frozen=True)
class ScanReportData:
    header: ReportHeader
    scan_spec: ScanCoordinateSpec | None
    attempts: tuple[AttemptReportRow, ...]
    segments: tuple[ScanSegment, ...]
    forward_barrier_kcal: float | None
    forward_drop_kcal: float | None
    imaginary_count: int | None
    mode_summaries: tuple[ModeSummary, ...]
    frequency_attempt_index: int | None


def collect_scan_report_data(
    state: Mapping[str, Any], route: RouteFacts, header: ReportHeader
) -> ScanReportData:
    """Collect the energy profile and vibrational summary of a relaxed scan."""
    # ``None`` for an unreadable coordinate: the profile is still a scan.
    scan_spec = first_scan_coordinate_spec(route.inp_path)
    attempts = attempt_dicts(state)
    surfaces = [
        tuple(parse_attempt_output(attempt, parse_scan_actual_surface) or ())
        for attempt in attempts
    ]
    rows = with_details(
        attempt_report_rows(attempts, "initial relaxed scan"),
        [str(len(points)) if points else "" for points in surfaces],
    )
    segments = tuple(
        ScanSegment(attempt_index=row.index, role=row.label, points=points)
        for row, points in zip(rows, surfaces, strict=True)
        if points
    )
    forward_energies = [point.energy for segment in segments for point in segment.points]

    forward_barrier = scan_profile_interior_barrier_kcal(forward_energies)
    forward_drop = (
        (forward_energies[-1] - forward_energies[0]) * KCAL_PER_HARTREE
        if len(forward_energies) >= 2
        else None
    )

    analysis, frequency_attempt_index = latest_frequency_analysis(attempts)
    alignment_pair: tuple[int, int] | None = None
    if scan_spec is not None and scan_spec.kind == "B" and len(scan_spec.atoms) == 2:
        alignment_pair = (scan_spec.atoms[0], scan_spec.atoms[1])

    return ScanReportData(
        header=header,
        scan_spec=scan_spec,
        attempts=rows,
        segments=segments,
        forward_barrier_kcal=forward_barrier,
        forward_drop_kcal=forward_drop,
        imaginary_count=analysis.imaginary_count() if analysis is not None else None,
        mode_summaries=mode_summaries(analysis, alignment_pair) if analysis is not None else (),
        frequency_attempt_index=frequency_attempt_index,
    )


def _profile_chart_svg(data: ScanReportData) -> str:
    all_energy = [point.energy for segment in data.segments for point in segment.points]
    if len(all_energy) < 2:
        return ""
    e_min = min(all_energy)

    series: list[ChartSeries] = []
    seen_roles: set[str] = set()
    for segment in data.segments:
        points = tuple(
            (point.coordinates[0], (point.energy - e_min) * KCAL_PER_HARTREE)
            for point in segment.points
            if point.coordinates
        )
        if not points:
            continue
        label = segment.role if segment.role not in seen_roles else ""
        seen_roles.add(segment.role)
        series.append(ChartSeries(label=label, color="#2f6fb2", dash="", points=points))

    x_label = "scan coordinate"
    if data.scan_spec is not None:
        unit = data.scan_spec.unit()
        x_label = f"{data.scan_spec.label()} / {unit}" if unit else data.scan_spec.label()
    return line_chart_svg(
        tuple(series),
        x_label=x_label,
        y_label="ΔE / kcal mol⁻¹",
    )


def _scan_range_html(spec: ScanCoordinateSpec | None) -> str:
    if spec is None:
        return ""
    unit = spec.unit()
    return (
        f" &#183; {html.escape(spec.label())} = "
        f"{spec.start:g} &#8594; {spec.end:g}"
        f"{' ' + html.escape(unit) if unit else ''}, "
        f"{spec.points} pt"
    )


def _metric_cards(data: ScanReportData, *, is_primary: bool, irc_present: bool) -> str:
    cards = []
    if data.forward_barrier_kcal is not None:
        barrier_note = "prominence over the shallower flank"
        cards.append(
            metric_card(
                "Interior barrier",
                f"{data.forward_barrier_kcal:.2f} <small>kcal/mol</small>",
                barrier_note,
            )
        )
    if data.forward_drop_kcal is not None:
        cards.append(
            metric_card(
                "Profile span",
                f"{data.forward_drop_kcal:+.1f} <small>kcal/mol</small>",
                "endpoint relative to scan start",
            )
        )
    if not irc_present and data.imaginary_count is not None:
        cards.append(
            metric_card(
                "Imaginary frequencies",
                str(data.imaginary_count),
                f"from attempt {data.frequency_attempt_index}"
                if data.frequency_attempt_index is not None
                else "",
            )
        )
    if is_primary:
        cards.append(attempts_metric_card(data.attempts, data.header.total_duration_text))
    return "".join(cards)


def scan_report_component(
    data: ScanReportData, *, is_primary: bool, irc_present: bool
) -> ReportComponent:
    chart = _profile_chart_svg(data) or (
        '<p class="muted">No relaxed-surface points were parsed from the attempt outputs.</p>'
    )
    alignment_label = data.scan_spec.label() if data.scan_spec is not None else None
    sections: list[tuple[str, str]] = [("Scan energy profile", chart)]
    if is_primary:
        attempts_html = attempts_table_html(data.attempts, "Scan points") + terminal_actions_html(
            data.attempts
        )
        sections.append(("Attempt chain", attempts_html))
    if not irc_present:
        sections.append(
            (
                "Vibrational summary",
                mode_section_html(
                    data.mode_summaries,
                    alignment_label,
                    frequency_calculation_found=data.frequency_attempt_index is not None,
                ),
            )
        )
    return ReportComponent(
        kind_label="Relaxed scan",
        badges=tuple(status_badges(data.header)),
        meta_html=job_meta_html(
            data.header, data.header.first_route_line, _scan_range_html(data.scan_spec)
        ),
        metrics_html=_metric_cards(data, is_primary=is_primary, irc_present=irc_present),
        sections=tuple(sections),
    )
