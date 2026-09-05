"""Single-point / bare-Freq job report: energy summary, attempt chain, SI block.

Covers exactly the jobs that get an ``"sp"`` SI block (routes without an
optimization: plain single points and bare Freq jobs). The rendered page
embeds the copy-paste-ready ``si_block.md`` content so the numbers a paper
needs can be copied straight from the browser.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..evidence import (
    OrcaEvidenceError,
    collect_structure_evidence,
    final_out_path,
    parsed_final_output,
)
from ..frequencies import (
    FrequencyAnalysis,
    ModeSummary,
    find_frequency_analysis,
    mode_summaries,
)
from ..input_blocks import file_route_lines
from ..parser import OrcaResult
from .attempts import (
    AttemptReportRow,
    attempt_dicts,
    attempt_report_rows,
    attempts_table_html,
    duration_text,
    terminal_actions_html,
)
from .frequencies import (
    mode_section_html,
)
from .render import (
    ReportComponent,
    job_meta_html,
    metric_card,
    status_badges,
)
from .si import (
    render_si_block_md,
)


@dataclass(frozen=True)
class SpReportData:
    title: str
    job_id: str
    status: str
    reason: str
    route_line: str
    started_at: str
    finished_at: str
    total_duration_text: str
    attempts: tuple[AttemptReportRow, ...]
    result: OrcaResult | None
    imaginary_count: int | None
    mode_summaries: tuple[ModeSummary, ...]
    si_block_text: str | None
    last_out_name: str


def collect_sp_report_data(reaction_dir: Path, state: Mapping[str, Any]) -> SpReportData | None:
    selected_raw = str(state.get("selected_inp") or "").strip()
    if not selected_raw:
        return None
    route_lines = file_route_lines(Path(selected_raw))
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
        analysis, _attempt_index = find_frequency_analysis(attempts)

    # The SI block only exists for completed jobs with a parsed energy and
    # geometry; the report is still useful without it (failed runs keep the
    # attempt chain), so its absence is not an error here.
    try:
        block = collect_structure_evidence(reaction_dir, state)
    except OrcaEvidenceError:
        block = None
    si_block_text = render_si_block_md(block) if block is not None else None

    final_result = state.get("final_result")
    final_payload: Mapping[str, Any] = final_result if isinstance(final_result, Mapping) else {}
    last_out = str(final_payload.get("last_out_path") or "").strip()
    if not last_out and attempts:
        last_out = str(attempts[-1].get("out_path") or "").strip()

    return SpReportData(
        title=reaction_dir.name or str(reaction_dir),
        job_id=str(state.get("job_id") or ""),
        status=str(state.get("status") or ""),
        reason=str(final_payload.get("reason") or ""),
        route_line=route_lines[0] if route_lines else "",
        started_at=str(state.get("started_at") or ""),
        finished_at=str(final_payload.get("completed_at") or ""),
        total_duration_text=duration_text(
            state.get("started_at"), final_payload.get("completed_at")
        ),
        attempts=rows,
        result=result,
        imaginary_count=analysis.imaginary_count() if analysis is not None else None,
        mode_summaries=mode_summaries(analysis, None) if analysis is not None else (),
        si_block_text=si_block_text,
        last_out_name=Path(last_out).name if last_out else "",
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


def sp_report_badges(data: SpReportData) -> tuple[tuple[str, str], ...]:
    return tuple(status_badges(data.status, data.reason))


def sp_report_meta_html(data: SpReportData) -> str:
    formula = data.result.formula if data.result is not None else ""
    formula_text = f" &#183; {html.escape(formula)}" if formula else ""
    version = data.result.orca_version if data.result is not None else ""
    version_text = f" &#183; ORCA {html.escape(version)}" if version else ""
    return job_meta_html(
        route_line=data.route_line,
        job_id=data.job_id,
        started_at=data.started_at,
        finished_at=data.finished_at,
        extra_html=formula_text + version_text,
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
        cards.append(
            metric_card("Charge / multiplicity", f"{result.charge} / {result.multiplicity}", note)
        )
    if data.imaginary_count is not None:
        cards.append(metric_card("Imaginary frequencies", str(data.imaginary_count), ""))
    cards.append(
        metric_card(
            "Attempts",
            str(len(data.attempts)),
            data.total_duration_text and f"total wall time {data.total_duration_text}",
        )
    )
    return "".join(cards)


def sp_report_component(data: SpReportData) -> ReportComponent:
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
    return ReportComponent(metrics_html=_metric_cards(data), sections=tuple(sections))


__all__ = [
    "SpReportData",
    "collect_sp_report_data",
    "sp_report_badges",
    "sp_report_component",
    "sp_report_meta_html",
]
