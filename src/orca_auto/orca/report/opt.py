"""Opt / OptTS / partial-Opt report: convergence trace, execution history, vibrational summary."""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..completion_rules import RouteFacts
from ..evidence import final_out_path, parsed_final_output
from ..frequencies import FrequencyAnalysis, ModeSummary, mode_summaries
from .attempts import (
    AttemptReportRow,
    attempt_dicts,
    attempt_index,
    attempt_out_path,
    attempt_report_rows,
    attempts_metric_card,
    attempts_table_html,
    latest_frequency_analysis,
    latest_optimization_progress,
    terminal_actions_html,
)
from .modes import mode_section_html
from .render import (
    ReportComponent,
    ReportHeader,
    job_meta_html,
    metric_card,
    relative_energy_cycle_chart_svg,
    status_badges,
)


@dataclass(frozen=True)
class OptReportData:
    header: ReportHeader
    kind: str
    formula: str
    method: str
    basis_set: str
    attempts: tuple[AttemptReportRow, ...]
    steps: tuple[tuple[int, float], ...]
    opt_converged: bool
    final_energy: float | None
    imaginary_count: int | None
    mode_summaries: tuple[ModeSummary, ...]
    frequency_attempt_index: int | None
    frequency_from_earlier_attempt: bool


_KIND_LABELS = {"ts": "TS", "partial": "Partial Opt"}


def collect_opt_report_data(
    state: Mapping[str, Any], route: RouteFacts, header: ReportHeader
) -> OptReportData:
    # A partial optimization (OptH, QMMMOpt, MECP-Opt, ...) is still an
    # optimization, but only a full one may be presented as a minimum.
    if route.is_ts:
        kind = "ts"
    elif route.is_full_opt:
        kind = "opt"
    else:
        kind = "partial"
    attempts = attempt_dicts(state)

    rows = attempt_report_rows(attempts, f"initial {'OptTS' if kind == 'ts' else 'Opt'}")

    formula = method = basis_set = ""
    steps: tuple[tuple[int, float], ...] = ()
    opt_converged = False
    final_energy: float | None = None
    # Prefer the latest attempt output that actually contains optimization
    # cycles; an execution that died before the first cycle parses to an empty
    # trace and must not mask an earlier attempt's convergence data.
    selected = latest_optimization_progress(attempts)
    if selected is not None:
        formula, method, basis_set = selected.formula, selected.method, selected.basis_set
        steps = tuple((step.cycle, step.energy_hartree) for step in selected.steps)
        opt_converged = selected.is_converged
        if steps:
            final_energy = steps[-1][1]

    # Characterize from the final output first so the card agrees with the
    # SI block, which reads only the final output; fall back to the attempt
    # chain only when the final output has no frequency section, and say so.
    analysis: FrequencyAnalysis | None = None
    frequency_attempt_index: int | None = None
    frequency_from_earlier_attempt = False
    out_path = final_out_path(state)
    if out_path is not None:
        try:
            _final_result, analysis = parsed_final_output(out_path)
        except OSError:
            analysis = None
    if analysis is not None and out_path is not None:
        frequency_attempt_index = _attempt_index_for_output(attempts, out_path)
    else:
        analysis, frequency_attempt_index = latest_frequency_analysis(attempts)
        frequency_from_earlier_attempt = analysis is not None

    return OptReportData(
        header=header,
        kind=kind,
        formula=formula,
        method=method,
        basis_set=basis_set,
        attempts=rows,
        steps=steps,
        opt_converged=opt_converged,
        final_energy=final_energy,
        imaginary_count=analysis.imaginary_count() if analysis is not None else None,
        mode_summaries=mode_summaries(analysis, None) if analysis is not None else (),
        frequency_attempt_index=frequency_attempt_index,
        frequency_from_earlier_attempt=frequency_from_earlier_attempt,
    )


def _attempt_index_for_output(attempts: Sequence[Mapping[str, Any]], out_path: Path) -> int | None:
    # Attempt rows store the path as written; the final result path can be
    # the resolved form of the same file. Compare resolved forms.
    target = _resolved_or_self(out_path)
    for position in range(len(attempts) - 1, -1, -1):
        attempt_out = attempt_out_path(attempts[position])
        if attempt_out is not None and _resolved_or_self(attempt_out) == target:
            return attempt_index(attempts[position], position)
    return None


def _resolved_or_self(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _imaginary_note(data: OptReportData) -> str:
    if data.imaginary_count is None:
        return ""
    notes: list[str] = []
    # A partial optimization ends on no stationary point of the full surface,
    # so its count carries no expectation.
    if data.kind != "partial":
        expected = 1 if data.kind == "ts" else 0
        kind_text = "TS" if data.kind == "ts" else "minimum"
        if data.imaginary_count == expected:
            notes.append(f"as expected for a {kind_text}")
        else:
            notes.append(f"expected {expected} for a {kind_text}")
    if data.frequency_from_earlier_attempt and data.frequency_attempt_index is not None:
        # The final output has no frequency section; the SI block will not
        # carry this count, so the card says where it came from.
        notes.append(f"from attempt {data.frequency_attempt_index}, not the final output")
    return "; ".join(notes)


def _metric_cards(data: OptReportData, *, is_primary: bool, irc_present: bool) -> str:
    # An IRC facet shows the final energy and frequencies itself.
    cards = []
    if not irc_present and data.final_energy is not None:
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
    if not irc_present and data.imaginary_count is not None:
        cards.append(
            metric_card(
                "Imaginary frequencies",
                str(data.imaginary_count),
                _imaginary_note(data),
            )
        )
    if is_primary:
        cards.append(attempts_metric_card(data.attempts, data.header.total_duration_text))
    return "".join(cards)


def opt_report_component(
    data: OptReportData, *, is_primary: bool, irc_present: bool
) -> ReportComponent:
    chart = relative_energy_cycle_chart_svg(data.steps) or (
        '<p class="muted">No optimization cycles were parsed from the attempt outputs.</p>'
    )
    section_title = (
        "TS optimization convergence" if data.kind == "ts" else "Optimization convergence"
    )
    sections: list[tuple[str, str]] = [(section_title, chart)]
    if is_primary:
        attempts_html = attempts_table_html(data.attempts, "Detail") + terminal_actions_html(
            data.attempts
        )
        sections.append(("Attempt chain", attempts_html))
    if not irc_present:
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
    formula_text = f" &#183; {html.escape(data.formula)}" if data.formula else ""
    return ReportComponent(
        kind_label=_KIND_LABELS.get(data.kind, "Opt"),
        badges=tuple(status_badges(data.header)),
        meta_html=job_meta_html(data.header, data.header.first_route_line, formula_text),
        metrics_html=_metric_cards(data, is_primary=is_primary, irc_present=irc_present),
        sections=tuple(sections),
    )
