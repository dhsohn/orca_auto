"""Collect verified workflow evidence for reports and machine observations."""

from __future__ import annotations

import os
import stat
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
from orca_auto.orca.out_analyzer import scan_ts_lines_for_imag_count
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


# Only stationary-point task kinds enter the ranked candidate table, so only
# they can publish a Nimag; both call sites say what that means for them.
_CANDIDATE_TASK_KINDS = frozenset({"opt", "optts_freq"})

# The analyzer counts imaginary modes only for a normally terminated TS route
# and clears the verdict for every other outcome, and these two reasons are
# exactly what it publishes in that case. They are also the only part of that
# decision the machine observation carries: ``summary.reason`` is cross-checked
# against ``final_result`` before a generation is accepted.
#
# A stage whose analyzer did reach a TS verdict can still be closed under a
# reason of the engine's own, with the last attempt's markers left intact:
# ``retry_limit_reached`` (state_machine), and ``scants_recipes_exhausted`` and
# ``rewrite_failed`` (attempt.retry, attempt.resume). Those publish no Nimag —
# each of those reasons is also produced by outcomes that characterized no
# stationary point, so none of them says which verdict produced the last
# output. No live generation here records one: the only job states carrying
# ``retry_limit_reached`` are fake-ORCA smoke fixtures with no machine
# observation, so none of them can reach this function.
_TS_VERDICT_REASONS = frozenset({"ts_criteria_met", "ts_criteria_failed"})


def _published_report_reason(report_payload: Mapping[str, Any]) -> str:
    """The reason as the machine observation's ``summary.reason`` pins it."""
    final_result = _mapping(_mapping(report_payload.get("engine_payload")).get("final_result"))
    status = _mapping(report_payload.get("status"))
    return report_diagnostics.normalized_text(final_result.get("reason") or status.get("reason"))


def _final_section_imaginary_count(
    generation_dir: Path,
    report_payload: Mapping[str, Any],
) -> int | None:
    """Recount one non-completed stage's Nimag from its hash-pinned output.

    The generation was accepted only after every artifact receipt was
    re-hashed, including the ``orca-output`` one that binds this ``.out``'s
    size and SHA-256, so the file is evidence the workflow report already
    stands behind. The engine's ``markers`` are not: nothing publishes or
    re-verifies them, so a hand-edited job state could dictate a Nimag.

    ``None`` means the stage characterizes no stationary point: the analyzer
    reached no TS verdict, the output is gone or is not a plain file of this
    generation, or its only frequency section was superseded by a later
    geometry.
    """
    if _published_report_reason(report_payload) not in _TS_VERDICT_REASONS:
        return None
    final_result = _mapping(_mapping(report_payload.get("engine_payload")).get("final_result"))
    out_text = report_diagnostics.normalized_text(final_result.get("last_out_path"))
    if not out_text:
        return None
    out_path = Path(out_text)
    # ORCA writes a stage's terminal output as a direct child of its
    # generation. Anything else is not this stage's output: a deeper path, a
    # sibling generation's, or — for a name that is not absolute — whatever the
    # report writer's working directory happens to hold under that name.
    if out_path.parent != generation_dir:
        return None
    return _stable_final_section_count(out_path)


def _stable_final_section_count(out_path: Path) -> int | None:
    """Count one output's final section, refusing anything the receipt misses.

    ``load_report_json`` recomputes the ``orca-output`` receipt from
    ``last_out_path`` and rejects the whole generation unless it equals the
    stored one, so an ``available`` receipt means these exact bytes were
    re-hashed when the generation was accepted. An ``invalid`` receipt matches
    on both sides while binding nothing, and a symlink, a hard link, a
    non-regular file or a path that is not its own resolved form is exactly
    what produces one — so those are refused here, together with a file
    substituted between the check and the open and bytes that moved under the
    scan. The checks live on the descriptor rather than on the path, which is
    what closes the window between checking and reading; the sibling energy
    reader in ``report_energy_evidence`` opens through a directory descriptor
    as well because it accepts an output at any depth under the generation,
    while this one has its parent pinned to the generation itself.
    """
    descriptor = -1
    try:
        # This stat exists only to pin an inode for the comparison below;
        # rejecting a symlinked output is O_NOFOLLOW's job, and O_NONBLOCK
        # keeps a path swapped for a FIFO from blocking the report writer
        # indefinitely.
        before = out_path.stat()
        descriptor = os.open(out_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        # The descriptor pins one inode; comparing it against the pre-open
        # stat rejects anything substituted between the two. A substitution
        # that happened before that stat is not covered — both sides would
        # then observe the substituted file and agree.
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            return None
        with open(descriptor, encoding="utf-8", errors="ignore", closefd=False) as handle:
            count, _irc_found, final_section = scan_ts_lines_for_imag_count(handle)
        # The receipt binds one (size, sha256). Bytes that moved under the scan
        # are no longer those bytes, so the count they produced is not the
        # evidence the observation stands behind.
        after = os.fstat(descriptor)
        if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None
        return count if final_section else None
    except (OSError, ValueError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _orca_stage_result(
    stage: Mapping[str, Any],
    workspace_dir: Path,
    *,
    candidate_task: bool,
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

    energy = None
    if authoritative_evidence is not None:
        block, _selected_input_identity = authoritative_evidence
        energy = block.result.energy_hartree
        imaginary_count = block.imaginary_count
    elif not stage_completed and report_json_path is not None and report_payload is not None:
        generation_dir = report_json_path.parent
        if candidate_task:
            # Only a candidate row carries a Nimag into the report, and the
            # recount reads the stage's terminal output whole. A relaxed scan
            # driven by ScanTS reaches a TS verdict too, so without this gate
            # it would pay for a count no row publishes.
            imaginary_count = _final_section_imaginary_count(generation_dir, report_payload)
        # A reusable job root can retain pre-generation ``*.engrad`` files.
        # Both energy sources must therefore be confined to the generation
        # whose report provenance and workflow-stage identity were verified.
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
                candidate_task = task_kind in _CANDIDATE_TASK_KINDS
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
                    candidate_task=candidate_task,
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
