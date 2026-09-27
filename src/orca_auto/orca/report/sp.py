"""Single-point / bare-Freq job report: energy summary, attempt chain, SI block.

Covers routes without an optimization (plain single points and bare Freq
jobs). Partial optimizations (OptH, QMMMOpt, MECP-Opt, ...) also get an
``"sp"`` SI block but the optimization report. The rendered page embeds the
copy-paste-ready ``si_block.md`` content so the numbers a paper needs can be
copied straight from the browser.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..completion_rules import RouteFacts
from ..evidence import (
    OrcaEvidenceError,
    collect_structure_evidence,
    final_out_path,
    parsed_final_output,
)
from ..frequencies import FrequencyAnalysis, ModeSummary, mode_summaries
from ..parser import OrcaResult
from .attempts import (
    AttemptReportRow,
    attempt_dicts,
    attempt_report_rows,
    attempts_metric_card,
    attempts_table_html,
    latest_frequency_analysis,
    terminal_actions_html,
)
from .modes import mode_section_html
from .render import (
    ReportComponent,
    ReportHeader,
    job_meta_html,
    metric_card,
    status_badges,
)
from .si import render_si_block_md


@dataclass(frozen=True)
class SpReportData:
    header: ReportHeader
    attempts: tuple[AttemptReportRow, ...]
    result: OrcaResult | None
    imaginary_count: int | None
    mode_summaries: tuple[ModeSummary, ...]
    si_block_text: str | None


def collect_sp_report_data(
    reaction_dir: Path, state: Mapping[str, Any], route: RouteFacts, header: ReportHeader
) -> SpReportData:
    attempts = attempt_dicts(state)
    rows = attempt_report_rows(attempts, "initial SP")

    out_path = final_out_path(state)
    result: OrcaResult | None = None
    analysis: FrequencyAnalysis | None = None
    if out_path is not None:
        try:
            result, analysis = parsed_final_output(out_path)
        except OSError:
            result, analysis = None, None

    # Characterize from the final output first so the metric cards agree with
    # the embedded SI block; fall back to the attempt chain only when the final
    # output has no frequency section (e.g. a failed run whose earlier attempt
    # still carries one).
    if analysis is None:
        analysis, _attempt_index = latest_frequency_analysis(attempts)

    # The SI block only exists for completed jobs with a parsed energy and
    # geometry; the report is still useful without it (failed runs keep the
    # attempt chain), so its absence is not an error here.
    try:
        block = collect_structure_evidence(reaction_dir, state, route)
    except OrcaEvidenceError:
        block = None
    si_block_text = render_si_block_md(block) if block is not None else None

    return SpReportData(
        header=header,
        attempts=rows,
        result=result,
        imaginary_count=analysis.imaginary_count() if analysis is not None else None,
        mode_summaries=mode_summaries(analysis, None) if analysis is not None else (),
        si_block_text=si_block_text,
    )


def _energy_rows(result: OrcaResult) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    if result.energy_hartree is not None:
        sub = ""
        if result.energy_ev is not None and result.energy_kcalmol is not None:
            sub = f"{result.energy_ev:.4f} eV · {result.energy_kcalmol:.2f} kcal·mol⁻¹"
        rows.append(("E(el)", f"{result.energy_hartree:.6f} Eh", sub))
    # Same rule as the SI block: no temperature label unless the output
    # actually stated one.
    temp = result.thermo_temperature_k
    temp_label = f" ({temp:.2f} K)" if temp is not None else ""
    for label, value in (
        ("ZPE correction", result.zpe_correction),
        (f"H{temp_label}", result.enthalpy),
        (f"G{temp_label}", result.gibbs_energy),
        ("G-E(el)", result.gibbs_correction),
    ):
        if value is not None:
            rows.append((label, f"{value:.6f} Eh", ""))
    return rows


def _energy_section_html(data: SpReportData) -> str:
    rows = _energy_rows(data.result) if data.result is not None else []
    if not rows:
        return '<p class="muted">No final energy was parsed from the attempt outputs.</p>'
    body = "".join(
        "<tr>"
        f"<td>{html.escape(label)}</td>"
        f"<td>{html.escape(value)}"
        + (f'<div class="sub">{html.escape(sub)}</div>' if sub else "")
        + "</td></tr>"
        for label, value, sub in rows
    )
    return (
        "<table><thead><tr><th>Quantity</th><th>Value</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _metric_cards(data: SpReportData) -> str:
    cards: list[str] = []
    result = data.result
    if result is not None and result.energy_hartree is not None:
        level = "/".join(part for part in (result.method, result.basis_set) if part)
        if result.solvation:
            level = f"{level} · {result.solvation}" if level else result.solvation
        cards.append(
            metric_card("Final energy", f"{result.energy_hartree:.6f} <small>Eh</small>", level)
        )
    if result is not None and result.n_atoms:
        note = " · ".join(part for part in (result.formula, f"{result.n_atoms} atoms") if part)
        electronic_state = (
            f"{result.charge} / {result.multiplicity}"
            if result.electronic_state_verified
            else "unavailable"
        )
        cards.append(metric_card("Charge / multiplicity", electronic_state, note))
    if data.imaginary_count is not None:
        cards.append(metric_card("Imaginary frequencies", str(data.imaginary_count), ""))
    cards.append(attempts_metric_card(data.attempts, data.header.total_duration_text))
    return "".join(cards)


def sp_report_component(data: SpReportData) -> ReportComponent:
    """The SP facet is only collected alone, so it is always the primary one."""
    sections: list[tuple[str, str]] = [("Energy summary", _energy_section_html(data))]
    if data.mode_summaries:
        sections.append(("Vibrational summary", mode_section_html(data.mode_summaries, None)))
    sections.append(
        (
            "Attempt chain",
            attempts_table_html(data.attempts, "Detail") + terminal_actions_html(data.attempts),
        )
    )
    if data.si_block_text:
        sections.append(
            (
                "SI block",
                f"<pre>{html.escape(data.si_block_text)}</pre>"
                '<p class="muted">Copy-paste-ready; also written to <code>si_block.md</code> '
                "next to this report.</p>",
            )
        )
    formula = data.result.formula if data.result is not None else ""
    formula_text = f" &#183; {html.escape(formula)}" if formula else ""
    version = data.result.orca_version if data.result is not None else ""
    version_text = f" &#183; ORCA {html.escape(version)}" if version else ""
    return ReportComponent(
        kind_label="SP",
        badges=tuple(status_badges(data.header)),
        meta_html=job_meta_html(
            data.header, data.header.first_route_line, formula_text + version_text
        ),
        metrics_html=_metric_cards(data),
        sections=tuple(sections),
    )


__all__ = [
    "SpReportData",
    "collect_sp_report_data",
    "sp_report_component",
]
