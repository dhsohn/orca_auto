"""Collect verified workflow evidence for reports and machine observations."""

from __future__ import annotations

import logging
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.engine_process import read_confined_text
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
from orca_auto.orca.parser.patterns import (
    FINAL_SINGLE_POINT_ENERGY_BYTES_RE,
    final_single_point_energy_value,
)
from orca_auto.orca.report.attempts import duration_text
from orca_auto.orca.report.si import SiBlock

from . import report_diagnostics
from .stage_summary import crest_stage_detail, stage_task_kind, xtb_stage_detail

# Preserve the established category while the implementation moves behind direct owners.
logger = logging.getLogger("orca_auto.flow.workflow.report")

_ENGRAD_ENERGY_MARKER = "current total energy"
_MAX_ENGRAD_ENERGY_FILE_BYTES = 8 * 1024 * 1024
_ORCA_ENERGY_SCAN_WINDOW_BYTES = 256 * 1024
# Consecutive scan windows overlap by this much so a line cut at a window's
# start is seen whole by the next window; it must exceed the longest possible
# final-energy line (marker + value + annotation, well under 200 bytes).
_ORCA_ENERGY_SCAN_OVERLAP_BYTES = 4 * 1024
# A window strictly larger than the overlap is what makes each backward step
# progress; equality would rescan the same window forever.
assert _ORCA_ENERGY_SCAN_WINDOW_BYTES > _ORCA_ENERGY_SCAN_OVERLAP_BYTES
_MAX_ORCA_ENERGY_CANDIDATES = 8
_ORCA_ENERGY_READ_CHUNK_BYTES = 64 * 1024


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


def latest_engrad_energy(directory: Path) -> float | None:
    """Total energy (Eh) from the most recent ``*.engrad`` in ``directory``."""
    try:
        resolved_directory = directory.expanduser().resolve(strict=True)
        directory_details = directory.lstat()
        if directory != resolved_directory or not stat.S_ISDIR(directory_details.st_mode):
            return None
        candidates: list[tuple[int, Path]] = []
        for entry in directory.glob("*.engrad"):
            details = entry.stat(follow_symlinks=False)
            if (
                stat.S_ISREG(details.st_mode)
                and details.st_nlink == 1
                and details.st_size <= _MAX_ENGRAD_ENERGY_FILE_BYTES
            ):
                candidates.append((int(details.st_mtime_ns), entry))
        candidates.sort(key=lambda item: item[0], reverse=True)
    except (OSError, RuntimeError):
        return None
    for _mtime_ns, candidate in candidates:
        try:
            lines = read_confined_text(
                resolved_directory,
                candidate,
                label="ORCA gradient energy",
                max_bytes=_MAX_ENGRAD_ENERGY_FILE_BYTES,
            ).splitlines()
        except (OSError, RuntimeError, ValueError):
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
                parsed = float(stripped)
            except ValueError:
                break
            # A corrupt .engrad can spell nan/inf; a non-finite energy must
            # not reach the report or the machine observation.
            return parsed if math.isfinite(parsed) else None
    return None


def _pread_exact(descriptor: int, offset: int, size: int) -> bytes | None:
    """Read exactly ``size`` bytes at ``offset``, or None on a short read."""
    buffer = bytearray()
    while len(buffer) < size:
        chunk = os.pread(
            descriptor,
            min(_ORCA_ENERGY_READ_CHUNK_BYTES, size - len(buffer)),
            offset + len(buffer),
        )
        if not chunk:
            break
        buffer.extend(chunk)
    if len(buffer) != size:
        return None
    return bytes(buffer)


def _last_final_energy_line_from_output(
    output_root: Path,
    candidate: Path,
) -> tuple[bool, bytes | None] | None:
    """Locate the file-final energy line of one confined, stable output.

    Scans fixed-size windows backwards from EOF until the newest complete
    ``FINAL SINGLE POINT ENERGY`` line is found, so the read cost is bounded
    by that line's distance from EOF rather than by a fixed tail; only an
    output that prints no final-energy line at all is read in full. Freq
    blocks routinely push that line several hundred KiB before EOF, which a
    single bounded tail cannot see past.

    Returns ``None`` when the output cannot be read safely, and
    ``(annotated, energy_text)`` otherwise; ``energy_text`` is
    ``None`` when no final-energy line exists or the last one is annotated.
    """
    parent_fd = -1
    output_fd = -1
    try:
        root = output_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
        if not relative.parts:
            return None

        candidate_before = os.stat(candidate, follow_symlinks=False)
        if not stat.S_ISREG(candidate_before.st_mode) or candidate_before.st_nlink != 1:
            return None

        # The parent is opened by its already-resolved path. O_NOFOLLOW rejects a
        # symlink only at the final component, so this does NOT prove by
        # construction that the parent is still inside the root — a walk from the
        # root would. That stronger guarantee is traded away deliberately: a
        # concurrent path swap by another process on this account is outside this
        # tool's declared operating model, and the inode comparison below covers
        # every swap that happens after the stat.
        directory_flags = os.O_RDONLY | os.O_CLOEXEC
        directory_flags |= os.O_DIRECTORY | os.O_NOFOLLOW
        parent_fd = os.open(resolved.parent, directory_flags)

        output_flags = os.O_RDONLY | os.O_CLOEXEC
        # O_NONBLOCK covers the stat-to-open race: without it a path swapped to a
        # FIFO between the two would block the report writer indefinitely.
        output_flags |= os.O_NONBLOCK | os.O_NOFOLLOW
        output_fd = os.open(relative.parts[-1], output_flags, dir_fd=parent_fd)
        opened = os.fstat(output_fd)
        # The fd pins one inode. Comparing it against the pre-open stat rejects
        # anything swapped between that stat and this open. A swap that already
        # happened before the stat is not covered: both sides would then observe
        # the substituted file and agree.
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (candidate_before.st_dev, candidate_before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            return None

        found: re.Match[bytes] | None = None
        window_end = opened.st_size
        while True:
            window_start = max(0, window_end - _ORCA_ENERGY_SCAN_WINDOW_BYTES)
            window = _pread_exact(output_fd, window_start, window_end - window_start)
            if window is None:
                return None
            for match in FINAL_SINGLE_POINT_ENERGY_BYTES_RE.finditer(window):
                # A window can start in the middle of a line. Do not treat that
                # truncated first line as a complete ORCA marker; the next
                # window's overlap re-reads it with its true line start.
                if window_start and match.start() == 0:
                    continue
                found = match
            if found is not None or window_start == 0:
                break
            window_end = window_start + _ORCA_ENERGY_SCAN_OVERLAP_BYTES

        after = os.fstat(output_fd)
        # A file that changed under the scan must be rejected, not parsed. The
        # energy marker's `$` also matches at the end of a window buffer, so a
        # half-written number would parse as a complete value and be displayed
        # as a wrong energy in the delta-E table.
        if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None
        if found is None:
            return False, None
        if found.group(2) is not None:
            return True, None
        return False, found.group(1)
    except (OSError, ValueError):
        return None
    finally:
        if output_fd >= 0:
            os.close(output_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _final_single_point_energy_from_output(
    output_root: Path, candidate: Path
) -> tuple[bool, float | None]:
    """Return ``(final_line_annotated, energy)`` for one confined output."""
    scan = _last_final_energy_line_from_output(output_root, candidate)
    if scan is None:
        return False, None
    annotated, energy_text = scan
    if annotated:
        # The final energy line is annotated ("(SCF not fully converged!)"):
        # an unconverged value must not feed the ΔE table, and any earlier
        # clean line belongs to a different geometry — forget it rather than
        # falling back to it.
        return True, None
    if energy_text is None:
        return False, None
    try:
        energy = final_single_point_energy_value(energy_text)
    except ValueError:
        return False, None
    return False, energy if math.isfinite(energy) else None


def _orca_report_output_energy_state(
    output_dir: Path,
    report_payload: Mapping[str, Any],
) -> tuple[bool, float | None]:
    """Return ``(final_line_annotated, energy)`` for the stage's output chain."""
    try:
        output_root = output_dir.resolve(strict=True)
    except OSError:
        return False, None

    engine_payload = _mapping(report_payload.get("engine_payload"))
    final_result = _mapping(engine_payload.get("final_result"))
    final_out_path = report_diagnostics.normalized_text(final_result.get("last_out_path"))
    candidates = [final_out_path]
    attempts = engine_payload.get("attempts")
    if isinstance(attempts, list):
        for attempt in reversed(attempts[-(_MAX_ORCA_ENERGY_CANDIDATES - 1) :]):
            if isinstance(attempt, Mapping):
                candidates.append(report_diagnostics.normalized_text(attempt.get("out_path")))

    seen: set[str] = set()
    for position, raw_path in enumerate(candidates):
        if not raw_path:
            continue
        candidate_key = os.path.abspath(raw_path)
        if candidate_key in seen:
            continue
        seen.add(candidate_key)
        annotated, energy = _final_single_point_energy_from_output(output_root, Path(raw_path))
        if annotated:
            # The newest readable output in the chain is tainted by an
            # unconverged SCF; older attempts must not stand in for it.
            return True, None
        if energy is not None:
            if final_out_path and position > 0:
                # A recorded final output is authoritative: when it is
                # missing, unreadable, or prints no final energy line, an
                # earlier attempt's clean value belongs to a different
                # geometry and must not stand in for it. Older attempts stay
                # consulted above as annotation evidence only — mirrors the
                # per-job final_out_path rule.
                return False, None
            return False, energy
    return False, None


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
        if attempts and isinstance(attempts[-1], dict):
            markers = attempts[-1].get("markers")
            if isinstance(markers, dict) and "imaginary_frequency_count" in markers:
                try:
                    imaginary_count = int(markers["imaginary_frequency_count"])
                except (TypeError, ValueError):
                    imaginary_count = None

    energy = None
    if authoritative_evidence is not None:
        block, _selected_input_identity = authoritative_evidence
        energy = block.result.energy_hartree
        imaginary_count = block.imaginary_count
    elif (
        report_diagnostics.normalized_text(stage.get("status")).lower() != "completed"
        and report_json_path is not None
        and report_payload is not None
    ):
        # A reusable job root can retain pre-generation ``*.engrad`` files.
        # Both energy sources must therefore be confined to the generation
        # whose report provenance and workflow-stage identity were verified.
        generation_dir = report_json_path.parent
        annotated_final, output_energy = _orca_report_output_energy_state(
            generation_dir, report_payload
        )
        if annotated_final:
            # A retained .engrad carries the same unconverged SCF's value and
            # cannot be cross-checked on its own — the annotation exists only
            # in the .out. Publish no energy for this stage.
            energy = None
        else:
            energy = latest_engrad_energy(generation_dir)
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
    "latest_engrad_energy",
]
