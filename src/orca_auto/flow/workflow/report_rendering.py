"""Render and publish workflow HTML from collected report data."""

from __future__ import annotations

import html
import logging
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import WORKFLOW_REPORT_HTML_FILE
from orca_auto.core.statuses import (
    FAILED_STATUSES,
    STATUS_CANCEL_FAILED,
    STATUS_COMPLETED,
    STATUS_FAILED,
)
from orca_auto.core.utils.persistence import atomic_write_text
from orca_auto.flow.workflow.report_collection import (
    WorkflowReportData,
    collect_workflow_report_data,
)
from orca_auto.orca.report.render import (
    ReportPage,
    metric_card,
    render_page,
    status_badge_kind,
)

# Preserve the established category while the implementation moves behind direct owners.
logger = logging.getLogger("orca_auto.flow.workflow.report")
_BAD_STATUS_CELL_STATUSES = frozenset({STATUS_CANCEL_FAILED, STATUS_FAILED})


def _status_cell_class(status: str) -> str:
    if status == STATUS_COMPLETED:
        return "ok"
    if status in _BAD_STATUS_CELL_STATUSES:
        return "bad"
    if status.startswith("cancel"):
        return "warn"
    return "warn"


def _metric_cards(data: WorkflowReportData) -> str:
    cards = [
        metric_card(
            "Stages",
            str(len(data.stage_rows)),
            data.total_duration_text and f"total wall time {data.total_duration_text}",
        )
    ]
    if data.crest_conformer_total is not None:
        cards.append(metric_card("CREST conformers", str(data.crest_conformer_total), ""))
    if data.xtb_candidate_total is not None:
        cards.append(metric_card("xTB candidates", str(data.xtb_candidate_total), ""))
    if data.orca_results:
        completed = sum(1 for entry in data.orca_results if entry.status == "completed")
        cards.append(
            metric_card(
                "ORCA jobs",
                f"{completed}<small>/{len(data.orca_results)}</small>",
                "completed",
            )
        )
    best = next(
        (
            entry
            for entry in data.orca_results
            if entry.energy is not None and entry.status == "completed"
        ),
        None,
    )
    if best is not None and best.energy is not None:
        cards.append(
            metric_card(
                "Best energy",
                f"{best.energy:.6f} <small>Eh</small>",
                best.label,
            )
        )
    return "".join(cards)


def _stage_table_html(data: WorkflowReportData) -> str:
    rows = []
    for position, row in enumerate(data.stage_rows, start=1):
        detail = html.escape(row.detail) if row.detail else "&#8211;"
        rows.append(
            "<tr>"
            f"<td>{position}</td>"
            f"<td>{html.escape(row.stage_id)}</td>"
            f"<td>{html.escape(row.stage_kind.removesuffix('_stage'))}</td>"
            f'<td class="{_status_cell_class(row.status)}">{html.escape(row.status)}</td>'
            f"<td>{detail}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>#</th><th>Stage</th><th>Engine</th><th>Status</th>"
        "<th>Detail</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _failure_table_html(data: WorkflowReportData) -> str:
    rows = []
    for failure in data.failure_rows:
        diagnostic = failure.explanation or failure.reason or "No engine reason was published."
        if failure.details_href:
            details = f'<a href="{html.escape(failure.details_href, quote=True)}">details</a>'
        else:
            details = "&#8211;"
        raw_reason = (
            f'<div class="sub">{html.escape(failure.reason)}</div>'
            if failure.reason and failure.reason != diagnostic
            else ""
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(failure.stage_id)}</td>"
            f"<td>{html.escape(failure.engine)}</td>"
            f'<td class="{_status_cell_class(failure.status)}">'
            f"{html.escape(failure.status)}</td>"
            f"<td>{html.escape(diagnostic)}{raw_reason}</td>"
            f"<td>{details}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Stage</th><th>Engine</th><th>Status</th>"
        "<th>Reason</th><th>Logs</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _failure_verdict_html(data: WorkflowReportData) -> str:
    if data.status not in FAILED_STATUSES:
        return ""
    if data.workflow_error_reason or data.workflow_error_message:
        reason = data.workflow_error_message or data.workflow_error_reason
        context_parts = []
        if data.workflow_error_stage_id:
            context_parts.append(f"stage: {data.workflow_error_stage_id}")
        if data.workflow_error_scope:
            context_parts.append(f"scope: {data.workflow_error_scope}")
        if (
            data.workflow_error_message
            and data.workflow_error_reason
            and data.workflow_error_message != data.workflow_error_reason
        ):
            context_parts.append(f"code: {data.workflow_error_reason}")
        context = (
            f' <span class="sub">{html.escape(" · ".join(context_parts))}</span>'
            if context_parts
            else ""
        )
        return (
            f'<p class="verdict"><strong>Why it failed:</strong> {html.escape(reason)}{context}</p>'
        )
    if data.failure_rows:
        primary = data.failure_rows[0]
        reason = primary.explanation or primary.reason or "No engine reason was published."
        details = ""
        if primary.details_href:
            details = (
                f' <a href="{html.escape(primary.details_href, quote=True)}">View details</a>.'
            )
        additional = len(data.failure_rows) - 1
        extra = f" {additional} additional failed stage(s)." if additional else ""
        return (
            '<p class="verdict"><strong>Why it failed:</strong> '
            f"{html.escape(primary.stage_id)} ({html.escape(primary.engine)}): "
            f"{html.escape(reason)}{details}{extra}</p>"
        )
    return (
        '<p class="verdict"><strong>Why it failed:</strong> '
        "The workflow ended in a failed state, but no detailed engine reason was published."
        "</p>"
    )


def _orca_table_html(data: WorkflowReportData) -> str:
    if not data.orca_results:
        return '<p class="muted">No ORCA stages in this workflow yet.</p>'
    comparable = [
        entry
        for entry in data.orca_results
        if entry.status == "completed" and entry.energy is not None
    ]
    provenance_note = ""
    comparison_omitted = bool(comparable) and all(entry.rel_kcal is None for entry in comparable)
    if comparison_omitted:
        provenance_note = (
            '<p class="muted">Relative energies are omitted because executed route/'
            "electronic-state provenance is missing or differs across completed candidates.</p>"
        )
    rows = []
    for rank, entry in enumerate(data.orca_results, start=1):
        rank_text = "&#8211;" if comparison_omitted else str(rank)
        energy = f"{entry.energy:.6f}" if entry.energy is not None else "&#8211;"
        rel = f"{entry.rel_kcal:+.2f}" if entry.rel_kcal is not None else "&#8211;"
        imag = str(entry.imaginary_count) if entry.imaginary_count is not None else "&#8211;"
        if entry.report_href is not None:
            label_html = (
                f'<a href="{html.escape(entry.report_href)}">{html.escape(entry.label)}</a>'
            )
        else:
            label_html = html.escape(entry.label)
        rows.append(
            "<tr>"
            f"<td>{rank_text}</td>"
            f'<td>{label_html}<div class="sub">{html.escape(entry.stage_id)}</div></td>'
            f'<td class="{_status_cell_class(entry.status)}">{html.escape(entry.status)}'
            f'<div class="sub">{html.escape(entry.reason)}</div></td>'
            f"<td>{energy}</td>"
            f"<td>{rel}</td>"
            f"<td>{imag}</td>"
            f"<td>{entry.attempt_count or '&#8211;'}</td>"
            "</tr>"
        )
    return provenance_note + (
        "<table><thead><tr><th>Rank</th><th>Candidate</th><th>Status</th>"
        "<th>E / Eh</th><th>&#916;E / kcal&#183;mol&#8315;&#185;</th>"
        "<th>Imag.</th><th>Attempts</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _energy_axis_ticks(max_value: float) -> tuple[float, ...]:
    target = max(max_value, 1.0)
    raw_step = target / 4
    magnitude = 10 ** math.floor(math.log10(raw_step))
    normalized = raw_step / magnitude
    for factor in (1.0, 2.0, 2.5, 5.0, 10.0):
        if normalized <= factor:
            step = factor * magnitude
            break
    tick_high = math.ceil(target / step) * step
    count = int(round(tick_high / step))
    return tuple(index * step for index in range(count + 1))


def _tick_label(value: float, step: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return str(int(round(value)))
    # Sub-0.5 steps (e.g. 0.25 when every candidate sits within 1 kcal/mol)
    # need two decimals; one decimal would label the 0.25 tick as "0.2".
    decimals = 1 if step >= 0.5 else 2
    return f"{value:.{decimals}f}"


def _short_candidate_label(rank: int, label: str, *, limit: int = 24) -> str:
    prefix = f"#{rank} "
    available = max(4, limit - len(prefix))
    shortened = label if len(label) <= available else label[: available - 3] + "..."
    return prefix + shortened


def _energy_lollipop_svg(data: WorkflowReportData) -> str:
    entries = tuple(
        (rank, entry)
        for rank, entry in enumerate(data.orca_results, start=1)
        if entry.rel_kcal is not None
    )
    if len(entries) < 2:
        return ""

    width = 760
    left, right, top, row_h = 150, 30, 30, 28
    axis_y = max(top + (len(entries) - 1) * row_h + 24, 136)
    height = axis_y + 38
    plot_w = width - left - right
    max_rel = max(entry.rel_kcal or 0.0 for _rank, entry in entries)
    ticks = _energy_axis_ticks(max_rel)
    x_high = ticks[-1]
    tick_step = ticks[1] - ticks[0] if len(ticks) > 1 else 1.0

    def sx(value: float) -> float:
        return left + value / x_high * plot_w

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        'style="width:100%;max-width:820px;display:block">',
    ]
    plot_top = top - 16
    for tick in ticks:
        x = sx(tick)
        parts.append(
            f'<line x1="{x:.1f}" y1="{plot_top}" x2="{x:.1f}" y2="{axis_y}" '
            'stroke="#e4e7ec" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{axis_y + 18}" text-anchor="middle" '
            f'font-size="11" fill="#69707c">{html.escape(_tick_label(tick, tick_step))}</text>'
        )
    parts.append(
        f'<line x1="{left}" y1="{axis_y}" x2="{left + plot_w}" y2="{axis_y}" '
        'stroke="#c8ccd4" stroke-width="1"/>'
    )

    for offset, (rank, entry) in enumerate(entries):
        rel = entry.rel_kcal or 0.0
        y = top + offset * row_h
        x = sx(rel)
        color = "#158a72" if offset == 0 else "#2f6fb2"
        label = _short_candidate_label(rank, entry.label)
        value_x = x + 8
        value_anchor = "start"
        if value_x > width - right - 42:
            value_x = x - 8
            value_anchor = "end"
        parts.append("<g>")
        parts.append(f"<title>{html.escape(entry.label)}: {rel:+.2f} kcal mol⁻¹</title>")
        parts.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#3d4451">{html.escape(label)}</text>'
        )
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
            'stroke="#b7bec9" stroke-width="1.5"/>'
        )
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}"/>')
        parts.append(
            f'<text x="{value_x:.1f}" y="{y + 4:.1f}" text-anchor="{value_anchor}" '
            f'font-size="11" fill="#3d4451">{rel:+.2f}</text>'
        )
        parts.append("</g>")
    parts.append(
        f'<text x="{left + plot_w / 2:.1f}" y="{height - 6}" text-anchor="middle" '
        f'font-size="12" fill="#3d4451">ΔE / kcal mol⁻¹</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def render_workflow_report_html(data: WorkflowReportData) -> str:
    badges = [(data.status or "unknown", status_badge_kind(data.status))]
    meta_parts = [f"template <code>{html.escape(data.template_name)}</code>"]
    if data.reaction_key:
        meta_parts.append(f"reaction <code>{html.escape(data.reaction_key)}</code>")
    meta_html = (
        " &#183; ".join(meta_parts)
        + f"<br>requested {html.escape(data.requested_at)}"
        + f" &#183; last advanced {html.escape(data.last_advanced_at)}"
    )

    sections: list[tuple[str, str]] = []
    if data.failure_rows:
        sections.append(("Stage failures", _failure_table_html(data)))
    sections.append(("Stage chain", _stage_table_html(data)))
    chart = _energy_lollipop_svg(data)
    orca_heading = (
        "TS candidates"
        if data.template_name in ("reaction_ts_search", "scan_ts_search")
        else "ORCA results"
    )
    sections.append((orca_heading, _orca_table_html(data)))
    if chart:
        sections.append(("Relative energies", chart))

    page = ReportPage(
        title=f"{data.workflow_id} · workflow report",
        badges=tuple(badges),
        meta_html=meta_html,
        verdict_html=_failure_verdict_html(data),
        metrics_html=_metric_cards(data),
        sections=tuple(sections),
        footer_html="Generated by orca_auto &#183; refreshed on every workflow advance",
    )
    return render_page(page)


def write_workflow_html_report(
    workspace_dir: Path,
    payload: Mapping[str, Any],
) -> Path | None:
    """Write ``workflow_report.html`` into the workspace; never raises."""
    try:
        data = collect_workflow_report_data(workspace_dir, payload)
        path = workspace_dir / WORKFLOW_REPORT_HTML_FILE
        atomic_write_text(path, render_workflow_report_html(data))
        return path
    except Exception:  # noqa: BLE001
        logger.warning(
            "Workflow HTML report generation failed for %s", workspace_dir, exc_info=True
        )
        return None


__all__ = [
    "render_workflow_report_html",
    "write_workflow_html_report",
]
