"""Opt / OptTS job report: convergence trace, retry chain, vibrational summary."""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..input_blocks import file_route_lines
from ..parser import parse_opt_progress
from .attempts import (
    AttemptReportRow,
    attempt_dicts,
    attempt_report_rows,
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
    line_chart_svg,
    metric_card,
    render_page,
    status_badge_kind,
    verdict_note,
)


@dataclass(frozen=True)
class OptReportData:
    title: str
    kind: str
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
    steps: tuple[tuple[int, float], ...]
    opt_converged: bool
    final_energy: float | None
    imaginary_count: int | None
    mode_summaries: tuple[ModeSummary, ...]
    frequency_attempt_index: int | None
    last_out_name: str

    def kind_label(self) -> str:
        return "TS" if self.kind == "ts" else "Opt"


def collect_opt_report_data(
    reaction_dir: Path,
    state: Mapping[str, Any],
    *,
    kind: str,
) -> OptReportData | None:
    selected_raw = str(state.get("selected_inp") or "").strip()
    if not selected_raw:
        return None
    selected_inp = Path(selected_raw)
    route_lines = file_route_lines(selected_inp)
    attempts = attempt_dicts(state)

    rows = attempt_report_rows(attempts, f"initial {'OptTS' if kind == 'ts' else 'Opt'}")

    formula = method = basis_set = ""
    steps: tuple[tuple[int, float], ...] = ()
    opt_converged = False
    final_energy: float | None = None
    for position in range(len(attempts) - 1, -1, -1):
        out_raw = str(attempts[position].get("out_path") or "").strip()
        if not out_raw or not Path(out_raw).exists():
            continue
        try:
            progress = parse_opt_progress(out_raw)
        except (OSError, FileNotFoundError):
            continue
        formula, method, basis_set = progress.formula, progress.method, progress.basis_set
        steps = tuple((step.cycle, step.energy_hartree) for step in progress.steps)
        opt_converged = progress.is_converged
        if steps:
            final_energy = steps[-1][1]
        break

    analysis, frequency_attempt_index = find_frequency_analysis(attempts)

    final_result = state.get("final_result")
    final_payload: Mapping[str, Any] = final_result if isinstance(final_result, Mapping) else {}
    last_out = str(final_payload.get("last_out_path") or "").strip()
    if not last_out and attempts:
        last_out = str(attempts[-1].get("out_path") or "").strip()

    return OptReportData(
        title=reaction_dir.name or str(reaction_dir),
        kind=kind,
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
        steps=steps,
        opt_converged=opt_converged,
        final_energy=final_energy,
        imaginary_count=analysis.imaginary_count() if analysis is not None else None,
        mode_summaries=mode_summaries(analysis, None) if analysis is not None else (),
        frequency_attempt_index=frequency_attempt_index,
        last_out_name=Path(last_out).name if last_out else "",
    )


def _convergence_chart_svg(data: OptReportData) -> str:
    if len(data.steps) < 2:
        return ""
    e_min = min(energy for _cycle, energy in data.steps)
    points = tuple(
        (float(cycle), (energy - e_min) * KCAL_PER_HARTREE) for cycle, energy in data.steps
    )
    series = (ChartSeries(label="", color="#2f6fb2", dash="", points=points),)
    return line_chart_svg(
        series,
        x_label="optimization cycle",
        y_label="ΔE / kcal mol⁻¹",
        x_tick_fmt=".0f",
    )


def _imaginary_note(data: OptReportData) -> str:
    if data.imaginary_count is None:
        return ""
    expected = 1 if data.kind == "ts" else 0
    kind_text = "TS" if data.kind == "ts" else "minimum"
    if data.imaginary_count == expected:
        return f"as expected for a {kind_text}"
    return f"expected {expected} for a {kind_text}"


def opt_report_badges(data: OptReportData) -> tuple[tuple[str, str], ...]:
    status_kind = status_badge_kind(data.status)
    badges: list[tuple[str, str]] = [(data.status or "unknown", status_kind)]
    if data.reason:
        badges.append((data.reason, "warn" if status_kind == "bad" else "muted"))
    return tuple(badges)


def opt_report_meta_html(data: OptReportData) -> str:
    formula_text = f" &#183; {html.escape(data.formula)}" if data.formula else ""
    return (
        f"<code>{html.escape(data.route_line)}</code>{formula_text}<br>"
        f"job <code>{html.escape(data.job_id)}</code>"
        f" &#183; started {html.escape(data.started_at)}"
        f" &#183; finished {html.escape(data.finished_at)}"
    )


def _metric_cards(
    data: OptReportData,
    *,
    include_attempts: bool = True,
    include_energy: bool = True,
    include_frequency: bool = True,
) -> str:
    cards = []
    if include_energy and data.final_energy is not None:
        cards.append(
            metric_card(
                "Final energy",
                f"{data.final_energy:.6f} <small>Eh</small>",
                f"{data.method}/{data.basis_set}" if data.method and data.basis_set else "",
            )
        )
    if data.steps:
        cards.append(
            metric_card(
                "TS opt cycles" if data.kind == "ts" else "Opt cycles",
                str(data.steps[-1][0]),
                "converged" if data.opt_converged else "not converged",
            )
        )
    if include_frequency and data.imaginary_count is not None:
        cards.append(
            metric_card(
                "Imaginary frequencies",
                str(data.imaginary_count),
                _imaginary_note(data),
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


def opt_report_component(
    data: OptReportData,
    *,
    include_attempt_metric: bool = True,
    include_attempt_chain: bool = True,
    include_energy_metric: bool = True,
    include_frequency_metric: bool = True,
    include_vibrational: bool = True,
) -> ReportComponent:
    chart = _convergence_chart_svg(data) or (
        '<p class="muted">No optimization cycles were parsed from the attempt outputs.</p>'
    )
    section_title = (
        "TS optimization convergence" if data.kind == "ts" else "Optimization convergence"
    )
    sections: list[tuple[str, str]] = [(section_title, chart)]
    if include_attempt_chain:
        attempts_html = attempts_table_html(data.attempts, "Detail") + terminal_actions_html(
            data.attempts
        )
        sections.append(("Attempt chain", attempts_html))
    if include_vibrational:
        sections.append(("Vibrational summary", mode_section_html(data.mode_summaries, None)))
    return ReportComponent(
        metrics_html=_metric_cards(
            data,
            include_attempts=include_attempt_metric,
            include_energy=include_energy_metric,
            include_frequency=include_frequency_metric,
        ),
        sections=tuple(sections),
    )


def render_opt_report_html(data: OptReportData) -> str:
    fallback_reason = data.attempts[-1].analyzer_reason if data.attempts else ""
    component = opt_report_component(data)

    page = ReportPage(
        title=f"{data.title} · {data.kind_label()} report",
        badges=opt_report_badges(data),
        meta_html=opt_report_meta_html(data),
        verdict_html=verdict_note(data.reason, fallback_reason),
        metrics_html=component.metrics_html,
        sections=component.sections,
        footer_html=(
            "Generated by orca_auto &#183; last output: "
            f"<code>{html.escape(data.last_out_name)}</code>"
        ),
    )
    return render_page(page)
