"""Self-contained HTML report for a workflow run.

``workflow_report.html`` is rewritten on every workflow advance, so it always
shows the live picture: stage chain with statuses, the CREST → (xTB) → ORCA
funnel, and a ranked table of ORCA results with relative energies and links to
the per-job ``job_report.html`` files. Works for both templates
(``conformer_screening`` ranks conformers, ``reaction_ts_search`` ranks TS
candidates).
"""

from __future__ import annotations

import html
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import RUN_REPORT_HTML_FILE, WORKFLOW_REPORT_HTML_FILE
from orca_auto.core.utils.persistence import atomic_write_text
from orca_auto.orca.report.attempts import duration_text
from orca_auto.orca.report.render import (
    KCAL_PER_HARTREE,
    ChartSeries,
    ReportPage,
    line_chart_svg,
    metric_card,
    render_page,
    status_badge_kind,
)

logger = logging.getLogger(__name__)

_ENGRAD_ENERGY_MARKER = "current total energy"


@dataclass(frozen=True)
class WorkflowStageRow:
    stage_id: str
    stage_kind: str
    status: str
    detail: str


@dataclass(frozen=True)
class OrcaStageResult:
    stage_id: str
    label: str
    status: str
    reason: str
    energy: float | None
    rel_kcal: float | None
    imaginary_count: int | None
    attempt_count: int
    report_href: str | None


@dataclass(frozen=True)
class WorkflowReportData:
    workflow_id: str
    template_name: str
    status: str
    reaction_key: str
    requested_at: str
    last_advanced_at: str
    total_duration_text: str
    stage_rows: tuple[WorkflowStageRow, ...]
    orca_results: tuple[OrcaStageResult, ...]
    crest_conformer_total: int | None
    xtb_candidate_total: int | None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _stage_dicts(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    stages = payload.get("stages")
    if not isinstance(stages, list):
        return []
    return [stage for stage in stages if isinstance(stage, dict)]


def _stage_metadata(stage: Mapping[str, Any]) -> dict[str, Any]:
    metadata = stage.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _stage_artifacts(stage: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    artifacts = stage.get("output_artifacts")
    if not isinstance(artifacts, list):
        return []
    return [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict) and _text(artifact.get("kind")) == kind
    ]


def _stage_artifact_path(stage: Mapping[str, Any], kind: str) -> Path | None:
    for artifact in _stage_artifacts(stage, kind):
        path_text = _text(artifact.get("path"))
        if path_text:
            return Path(path_text)
    return None


def count_xyz_frames(path: Path) -> int | None:
    """Frames in a concatenated-XYZ file; ``None`` when unreadable/malformed."""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            first = handle.readline().strip()
            if not first.isdigit():
                return None
            atoms = int(first)
            if atoms <= 0:
                return None
            total_lines = 1 + sum(1 for _ in handle)
    except OSError:
        return None
    frame_lines = atoms + 2
    return total_lines // frame_lines if total_lines >= frame_lines else None


def latest_engrad_energy(directory: Path) -> float | None:
    """Total energy (Eh) from the most recent ``*.engrad`` in ``directory``."""
    try:
        candidates = sorted(
            directory.glob("*.engrad"),
            key=lambda entry: entry.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for candidate in candidates:
        try:
            lines = candidate.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        marker_seen = False
        for line in lines:
            stripped = line.strip()
            if _ENGRAD_ENERGY_MARKER in stripped.lower():
                marker_seen = True
                continue
            if not marker_seen or not stripped or stripped.startswith("#"):
                continue
            try:
                return float(stripped)
            except ValueError:
                break
    return None


def _crest_stage_detail(stage: Mapping[str, Any]) -> tuple[str, int | None]:
    metadata = _stage_metadata(stage)
    conformers_path = None
    for artifact in _stage_artifacts(stage, "crest_conformer"):
        path_text = _text(artifact.get("path"))
        if path_text.endswith("crest_conformers.xyz"):
            conformers_path = Path(path_text)
            break
    frames = count_xyz_frames(conformers_path) if conformers_path is not None else None
    parts = []
    role = _text(metadata.get("input_role"))
    if role:
        parts.append(role)
    mode = _text(metadata.get("mode"))
    if mode:
        parts.append(f"mode {mode}")
    if frames is not None:
        parts.append(f"{frames} conformers")
    return " · ".join(parts), frames


def _xtb_stage_detail(stage: Mapping[str, Any]) -> tuple[str, int]:
    metadata = _stage_metadata(stage)
    candidates = _stage_artifacts(stage, "xtb_candidate")
    kinds = [_text((artifact.get("metadata") or {}).get("kind")) for artifact in candidates]
    parts = []
    reaction_key = _text(metadata.get("reaction_key"))
    if reaction_key:
        parts.append(reaction_key)
    if candidates:
        kind_text = ", ".join(kind for kind in kinds if kind)
        parts.append(f"{len(candidates)} candidates" + (f" ({kind_text})" if kind_text else ""))
    return " · ".join(parts), len(candidates)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _orca_stage_output_dir(stage: Mapping[str, Any]) -> Path | None:
    for kind in ("orca_output_dir",):
        path = _stage_artifact_path(stage, kind)
        if path is not None:
            return path
    reaction_dir = _text(_stage_metadata(stage).get("reaction_dir"))
    if reaction_dir:
        return Path(reaction_dir)
    report_json = _stage_artifact_path(stage, "orca_report_json")
    if report_json is not None:
        return report_json.parent
    return None


def _orca_stage_result(stage: Mapping[str, Any], workspace_dir: Path) -> OrcaStageResult:
    metadata = _stage_metadata(stage)
    stage_id = _text(stage.get("stage_id"))
    label = _text(metadata.get("selected_input_label")) or stage_id

    reason = ""
    attempt_count = 0
    imaginary_count: int | None = None
    report_json_path = _stage_artifact_path(stage, "orca_report_json")
    report_payload = _load_json(report_json_path) if report_json_path is not None else None
    if report_payload is not None:
        engine_payload = report_payload.get("engine_payload")
        engine_payload = engine_payload if isinstance(engine_payload, dict) else {}
        final_result = engine_payload.get("final_result")
        final_result = final_result if isinstance(final_result, dict) else {}
        reason = _text(final_result.get("reason"))
        attempts = engine_payload.get("attempts")
        attempts = attempts if isinstance(attempts, list) else []
        attempt_count = len(attempts)
        if attempts and isinstance(attempts[-1], dict):
            markers = attempts[-1].get("markers")
            if isinstance(markers, dict) and "imaginary_frequency_count" in markers:
                try:
                    imaginary_count = int(markers["imaginary_frequency_count"])
                except (TypeError, ValueError):
                    imaginary_count = None

    output_dir = _orca_stage_output_dir(stage)
    energy = latest_engrad_energy(output_dir) if output_dir is not None else None

    report_href: str | None = None
    if output_dir is not None:
        job_report_html = output_dir / RUN_REPORT_HTML_FILE
        if job_report_html.exists():
            try:
                report_href = os.path.relpath(job_report_html, workspace_dir)
            except ValueError:
                report_href = str(job_report_html)

    return OrcaStageResult(
        stage_id=stage_id,
        label=label,
        status=_text(stage.get("status")),
        reason=reason,
        energy=energy,
        rel_kcal=None,
        imaginary_count=imaginary_count,
        attempt_count=attempt_count,
        report_href=report_href,
    )


def _with_relative_energies(results: list[OrcaStageResult]) -> tuple[OrcaStageResult, ...]:
    """Rank results by energy with completed stages first.

    Only completed stages enter the ΔE baseline and carry a ``rel_kcal``: a
    failed or cancelled stage's ``.engrad`` holds a transient (non-stationary)
    energy, which must not become the reference point nor rank as if it were a
    valid candidate. Their raw energy still shows in the table.
    """
    completed_energies = [
        entry.energy
        for entry in results
        if entry.energy is not None and entry.status == "completed"
    ]
    if not completed_energies:
        return tuple(results)
    best = min(completed_energies)
    ranked = [
        entry
        if entry.energy is None or entry.status != "completed"
        else replace(entry, rel_kcal=(entry.energy - best) * KCAL_PER_HARTREE)
        for entry in results
    ]
    ranked.sort(
        key=lambda entry: (
            entry.status != "completed",
            entry.energy is None,
            entry.energy or 0.0,
        )
    )
    return tuple(ranked)


def collect_workflow_report_data(
    workspace_dir: Path,
    payload: Mapping[str, Any],
) -> WorkflowReportData:
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    requested_at = _text(payload.get("requested_at"))
    last_advanced_at = _text(metadata.get("last_advanced_at"))

    stage_rows: list[WorkflowStageRow] = []
    orca_results: list[OrcaStageResult] = []
    crest_total: int | None = None
    xtb_total: int | None = None
    for stage in _stage_dicts(payload):
        stage_kind = _text(stage.get("stage_kind"))
        detail = ""
        if stage_kind == "crest_stage":
            detail, frames = _crest_stage_detail(stage)
            if frames is not None:
                crest_total = (crest_total or 0) + frames
        elif stage_kind == "xtb_stage":
            detail, candidates = _xtb_stage_detail(stage)
            xtb_total = (xtb_total or 0) + candidates
        elif stage_kind == "orca_stage":
            result = _orca_stage_result(stage, workspace_dir)
            orca_results.append(result)
            detail_parts = [part for part in (result.label, result.reason) if part]
            detail = " · ".join(detail_parts)
        stage_rows.append(
            WorkflowStageRow(
                stage_id=_text(stage.get("stage_id")),
                stage_kind=stage_kind,
                status=_text(stage.get("status")),
                detail=detail,
            )
        )

    return WorkflowReportData(
        workflow_id=_text(payload.get("workflow_id")),
        template_name=_text(payload.get("template_name")),
        status=_text(payload.get("status")),
        reaction_key=_text(payload.get("reaction_key")),
        requested_at=requested_at,
        last_advanced_at=last_advanced_at,
        total_duration_text=duration_text(requested_at, last_advanced_at),
        stage_rows=tuple(stage_rows),
        orca_results=_with_relative_energies(orca_results),
        crest_conformer_total=crest_total,
        xtb_candidate_total=xtb_total,
    )


def _status_cell_class(status: str) -> str:
    if status == "completed":
        return "ok"
    if status in {"failed", "cancel_failed"}:
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


def _orca_table_html(data: WorkflowReportData) -> str:
    if not data.orca_results:
        return '<p class="muted">No ORCA stages in this workflow yet.</p>'
    rows = []
    for rank, entry in enumerate(data.orca_results, start=1):
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
            f"<td>{rank}</td>"
            f'<td>{label_html}<div class="sub">{html.escape(entry.stage_id)}</div></td>'
            f'<td class="{_status_cell_class(entry.status)}">{html.escape(entry.status)}'
            f'<div class="sub">{html.escape(entry.reason)}</div></td>'
            f"<td>{energy}</td>"
            f"<td>{rel}</td>"
            f"<td>{imag}</td>"
            f"<td>{entry.attempt_count or '&#8211;'}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Rank</th><th>Candidate</th><th>Status</th>"
        "<th>E / Eh</th><th>&#916;E / kcal&#183;mol&#8315;&#185;</th>"
        "<th>Imag.</th><th>Attempts</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _energy_chart_svg(data: WorkflowReportData) -> str:
    points = tuple(
        (float(rank), entry.rel_kcal)
        for rank, entry in enumerate(data.orca_results, start=1)
        if entry.rel_kcal is not None
    )
    if len(points) < 2:
        return ""
    series = (ChartSeries(label="", color="#2f6fb2", dash="", points=points),)
    return line_chart_svg(
        series,
        x_label="rank",
        y_label="ΔE / kcal mol⁻¹",
        x_tick_fmt=".0f",
    )


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

    sections: list[tuple[str, str]] = [("Stage chain", _stage_table_html(data))]
    chart = _energy_chart_svg(data)
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
        verdict_html="",
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
    "OrcaStageResult",
    "WorkflowReportData",
    "WorkflowStageRow",
    "collect_workflow_report_data",
    "count_xyz_frames",
    "latest_engrad_energy",
    "render_workflow_report_html",
    "write_workflow_html_report",
]
