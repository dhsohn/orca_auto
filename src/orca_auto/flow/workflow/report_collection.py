"""Collect verified workflow evidence for reports and machine observations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.statuses import FAILED_STATUSES
from orca_auto.core.utils import mapping_or_empty as _mapping
from orca_auto.flow.conformer_selection import OrcaSelectedInputScienceIdentity
from orca_auto.flow.contracts.workflow import (
    is_orca_stage_kind,
    is_supported_orca_stage_contract,
    workflow_stage_dicts,
)
from orca_auto.flow.orca_stage_evidence import (
    collect_verified_orca_stage_evidence,
)
from orca_auto.flow.orca_stage_evidence import (
    stage_metadata as _stage_metadata,
)
from orca_auto.flow.orca_stage_evidence import (
    stage_task as _stage_task,
)
from orca_auto.orca.parser import KCAL_PER_HARTREE
from orca_auto.orca.report.attempts import duration_text
from orca_auto.orca.report.si import SiBlock

from . import report_diagnostics, report_energy_evidence
from .stage_summary import crest_stage_detail, stage_task_kind, xtb_stage_detail


@dataclass(frozen=True)
class WorkflowStageRow:
    stage_id: str
    stage_kind: str
    status: str
    detail: str


@dataclass(frozen=True)
class WorkflowFailureRow:
    stage_id: str
    engine: str
    status: str
    reason: str
    explanation: str
    details_href: str | None


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
    machine_path: Path | None = None
    science_identity: tuple[OrcaSelectedInputScienceIdentity, str] | None = None


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
    failure_rows: tuple[WorkflowFailureRow, ...]
    workflow_error_reason: str
    workflow_error_message: str
    workflow_error_scope: str
    workflow_error_stage_id: str
    orca_results: tuple[OrcaStageResult, ...]
    crest_conformer_total: int | None
    xtb_candidate_total: int | None
    consumed_orca_machine_paths: tuple[Path, ...] = ()


def _orca_stage_result(
    stage: Mapping[str, Any],
    workspace_dir: Path,
    *,
    authoritative_evidence: tuple[
        SiBlock,
        OrcaSelectedInputScienceIdentity | None,
    ]
    | None = None,
) -> OrcaStageResult:
    metadata = _stage_metadata(stage)
    stage_id = report_diagnostics.normalized_text(stage.get("stage_id"))
    label = report_diagnostics.normalized_text(metadata.get("selected_input_label")) or stage_id

    reason = ""
    attempt_count = 0
    imaginary_count: int | None = None
    stage_completed = report_diagnostics.normalized_text(stage.get("status")).lower() == "completed"
    report_json_path, report_payload = report_diagnostics.resolve_stage_job_report(stage)
    if report_payload is not None:
        engine_payload = report_payload.get("engine_payload")
        engine_payload = engine_payload if isinstance(engine_payload, dict) else {}
        final_result = engine_payload.get("final_result")
        final_result = final_result if isinstance(final_result, dict) else {}
        reason = report_diagnostics.normalized_text(final_result.get("reason"))
        attempts = engine_payload.get("attempts")
        attempts = attempts if isinstance(attempts, list) else []
        attempt_count = len(attempts)
        # The analyzer's imaginary-mode count is a Nimag only when the
        # analyzer marked it as a verdict on the final geometry: counted in the
        # frequency section after the last final energy and the TS criteria
        # were reached (a normally terminated candidate rejected for 0 or 2
        # modes). For a run that stopped short or failed, or a record written
        # before the marker existed, the count says nothing about the final
        # geometry and stays unknown.
        if attempts and isinstance(attempts[-1], dict):
            markers = attempts[-1].get("markers")
            if (
                isinstance(markers, dict)
                and markers.get("final_frequency_section") is True
                and "imaginary_frequency_count" in markers
            ):
                try:
                    imaginary_count = int(markers["imaginary_frequency_count"])
                except (TypeError, ValueError):
                    imaginary_count = None

    energy = None
    if authoritative_evidence is not None:
        block, _selected_input_identity = authoritative_evidence
        energy = block.result.energy_hartree
        imaginary_count = block.imaginary_count
    elif not stage_completed and report_json_path is not None and report_payload is not None:
        # A reusable job root can retain pre-generation ``*.engrad`` files.
        # Both energy sources must therefore be confined to the generation
        # whose report provenance and workflow-stage identity were verified.
        generation_dir = report_json_path.parent
        annotated_final, output_energy = report_energy_evidence.orca_report_output_energy_state(
            generation_dir, report_payload
        )
        if annotated_final:
            # A retained .engrad carries the same unconverged SCF's value and
            # cannot be cross-checked on its own — the annotation exists only
            # in the .out. Publish no energy for this stage.
            energy = None
        else:
            energy = report_energy_evidence.latest_engrad_energy(generation_dir)
            if energy is None:
                energy = output_energy

    report_href: str | None = None
    if report_json_path is not None:
        # The HTML report is co-located with the generation-local report JSON.
        report_href = report_diagnostics.report_html_href(report_json_path, workspace_dir)

    science_identity = None
    if authoritative_evidence is not None:
        block, selected_input_identity = authoritative_evidence
        orca_version = block.result.orca_version.strip()
        if (
            selected_input_identity is not None
            and orca_version
            and block.result.electronic_state_verified
        ):
            science_identity = (selected_input_identity, orca_version)

    return OrcaStageResult(
        stage_id=stage_id,
        label=label,
        status=report_diagnostics.normalized_text(stage.get("status")),
        reason=reason,
        energy=energy,
        rel_kcal=None,
        imaginary_count=imaginary_count,
        attempt_count=attempt_count,
        report_href=report_href,
        machine_path=report_json_path if report_payload is not None else None,
        science_identity=science_identity,
    )


def _with_relative_energies(results: list[OrcaStageResult]) -> tuple[OrcaStageResult, ...]:
    """Rank scientifically comparable results by energy with completed stages first.

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
    comparable = [
        entry for entry in results if entry.energy is not None and entry.status == "completed"
    ]
    science_identities = {entry.science_identity for entry in comparable}
    if None in science_identities or len(science_identities) != 1:
        # Absolute energies remain useful diagnostics, but a cross-level ΔE
        # has no physical meaning. Preserve workflow order instead of silently
        # ranking the candidates by those incomparable values.
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
    requested_at = report_diagnostics.normalized_text(payload.get("requested_at"))
    last_advanced_at = report_diagnostics.normalized_text(metadata.get("last_advanced_at"))

    stage_rows: list[WorkflowStageRow] = []
    failure_rows: list[WorkflowFailureRow] = []
    orca_results: list[OrcaStageResult] = []
    consumed_orca_machine_paths: list[Path] = []
    crest_total: int | None = None
    xtb_total: int | None = None
    for stage in workflow_stage_dicts(payload):
        stage_kind = report_diagnostics.normalized_text(stage.get("stage_kind"))
        stage_status = report_diagnostics.normalized_text(stage.get("status")).lower()
        task = _stage_task(stage)
        task_status = report_diagnostics.normalized_text(task.get("status")).lower()
        engine = report_diagnostics.normalized_text(
            task.get("engine")
        ).lower() or stage_kind.removesuffix("_stage")
        reason, explanation, details_href = report_diagnostics.collect_stage_diagnostic(
            stage,
            workspace_dir,
        )
        detail = ""
        if stage_kind == "crest_stage":
            detail, frames = crest_stage_detail(stage)
            if frames is not None:
                crest_total = (crest_total or 0) + frames
        elif stage_kind == "xtb_stage":
            detail, candidates = xtb_stage_detail(stage)
            xtb_total = (xtb_total or 0) + candidates
        elif is_orca_stage_kind(stage):
            if is_supported_orca_stage_contract(stage):
                task_kind = stage_task_kind(stage)
                candidate_task = task_kind in {"opt", "optts_freq"}
                authoritative_evidence = None
                evidence_reason = ""
                if candidate_task and stage_status == "completed":
                    block, evidence_reason, selected_input_identity = (
                        collect_verified_orca_stage_evidence(stage)
                    )
                    if block is not None:
                        authoritative_evidence = (block, selected_input_identity)
                result = _orca_stage_result(
                    stage,
                    workspace_dir,
                    authoritative_evidence=authoritative_evidence,
                )
                if result.machine_path is not None and (
                    not candidate_task
                    or stage_status != "completed"
                    or authoritative_evidence is not None
                ):
                    consumed_orca_machine_paths.append(result.machine_path)
                # Only stationary-point task kinds enter the ranked candidate
                # table. Relaxed scans are prerequisites, while ordinary and
                # interaction single points are SI refinement inputs.
                if candidate_task and (
                    stage_status != "completed" or authoritative_evidence is not None
                ):
                    orca_results.append(result)
                detail_parts = [
                    part
                    for part in (
                        result.label,
                        result.reason,
                        evidence_reason if stage_status == "completed" else "",
                    )
                    if part
                ]
                detail = " · ".join(detail_parts)
            else:
                detail = "unsupported or contradictory ORCA stage contract"
        diagnostic_detail = explanation or reason
        if diagnostic_detail and diagnostic_detail not in detail:
            detail = " · ".join(part for part in (detail, diagnostic_detail) if part)
        if stage_status in FAILED_STATUSES or task_status in FAILED_STATUSES:
            failure_rows.append(
                WorkflowFailureRow(
                    stage_id=report_diagnostics.normalized_text(stage.get("stage_id")),
                    engine=engine,
                    status=(stage_status if stage_status in FAILED_STATUSES else task_status),
                    reason=reason,
                    explanation=explanation,
                    details_href=details_href,
                )
            )
        stage_rows.append(
            WorkflowStageRow(
                stage_id=report_diagnostics.normalized_text(stage.get("stage_id")),
                stage_kind=stage_kind,
                status=stage_status,
                detail=detail,
            )
        )

    workflow_error = _mapping(metadata.get("workflow_error"))

    return WorkflowReportData(
        workflow_id=report_diagnostics.normalized_text(payload.get("workflow_id")),
        template_name=report_diagnostics.normalized_text(payload.get("template_name")),
        status=report_diagnostics.normalized_text(payload.get("status")),
        reaction_key=report_diagnostics.normalized_text(payload.get("reaction_key")),
        requested_at=requested_at,
        last_advanced_at=last_advanced_at,
        total_duration_text=duration_text(requested_at, last_advanced_at),
        stage_rows=tuple(stage_rows),
        failure_rows=tuple(failure_rows),
        workflow_error_reason=report_diagnostics.normalized_text(workflow_error.get("reason")),
        workflow_error_message=report_diagnostics.normalized_text(workflow_error.get("message")),
        workflow_error_scope=report_diagnostics.normalized_text(workflow_error.get("scope")),
        workflow_error_stage_id=report_diagnostics.normalized_text(workflow_error.get("stage_id")),
        orca_results=_with_relative_energies(orca_results),
        crest_conformer_total=crest_total,
        xtb_candidate_total=xtb_total,
        consumed_orca_machine_paths=tuple(consumed_orca_machine_paths),
    )


__all__ = [
    "OrcaStageResult",
    "WorkflowFailureRow",
    "WorkflowReportData",
    "WorkflowStageRow",
    "collect_workflow_report_data",
]
