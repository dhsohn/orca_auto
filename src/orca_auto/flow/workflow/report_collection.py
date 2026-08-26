"""Collect verified workflow evidence for reports and machine observations."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import RUN_REPORT_HTML_FILE, RUN_REPORT_JSON_FILE
from orca_auto.core.engine_process import read_confined_text
from orca_auto.core.statuses import FAILED_STATUSES, STATUS_CANCELLED
from orca_auto.core.utils import mapping_or_empty as _mapping
from orca_auto.flow.conformer_selection import OrcaSelectedInputScienceIdentity
from orca_auto.flow.contracts.workflow import (
    is_orca_stage_kind,
    is_supported_orca_stage_contract,
    workflow_stage_dicts,
)
from orca_auto.flow.orca_stage_evidence import (
    collect_verified_orca_stage_evidence,
    resolve_verified_orca_stage_report,
    stage_report_identity_matches,
)
from orca_auto.flow.orca_stage_evidence import (
    stage_job_dirs as _stage_job_dirs,
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
_DIAGNOSTIC_STAGE_STATUSES = frozenset({*FAILED_STATUSES, STATUS_CANCELLED})
_INTERNAL_STAGE_ENGINES = {
    "crest_stage": "crest",
    "xtb_stage": "xtb",
}


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


def _text(value: Any) -> str:
    return str(value or "").strip()


def _stage_has_diagnostic_status(stage: Mapping[str, Any]) -> bool:
    return (
        _text(stage.get("status")).lower() in _DIAGNOSTIC_STAGE_STATUSES
        or _text(_stage_task(stage).get("status")).lower() in _DIAGNOSTIC_STAGE_STATUSES
    )


def _task_kind(stage: Mapping[str, Any]) -> str:
    task = stage.get("task")
    if not isinstance(task, dict):
        return ""
    return _text(task.get("task_kind"))


def _stage_artifacts(stage: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    artifacts = stage.get("output_artifacts")
    if not isinstance(artifacts, list):
        return []
    return [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict) and _text(artifact.get("kind")) == kind
    ]


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


def _stage_job_report(stage: Mapping[str, Any]) -> tuple[Path | None, dict[str, Any] | None]:
    """Resolve the canonical verified report for one workflow stage."""
    task_engine = _text(_stage_task(stage).get("engine")).lower()
    internal_engine = _INTERNAL_STAGE_ENGINES.get(_text(stage.get("stage_kind")).lower())
    if internal_engine is not None:
        if task_engine != internal_engine:
            return None, None
        for job_dir in _stage_job_dirs(stage):
            state_path = job_dir / "job_state.json"
            state = _load_json(state_path)
            if (
                state is not None
                and _text(state.get("engine")).lower() == internal_engine
                and stage_report_identity_matches(
                    stage,
                    state,
                    require_job_and_run=False,
                )
            ):
                return state_path, state
        return None, None
    if task_engine in _INTERNAL_STAGE_ENGINES.values():
        return None, None
    return resolve_verified_orca_stage_report(stage)


def _stage_status_reason(stage: Mapping[str, Any], report: Mapping[str, Any] | None) -> str:
    metadata = _stage_metadata(stage)
    task = _stage_task(stage)
    report_status = _mapping(report.get("status")) if report is not None else {}
    for value in (
        metadata.get("reaction_handoff_reason"),
        metadata.get("reason"),
        report_status.get("reason"),
        _mapping(task.get("cancel_result")).get("reason"),
        _mapping(task.get("submission_result")).get("reason"),
        metadata.get("submission_deferred_reason"),
    ):
        reason = _text(value)
        if reason:
            return reason
    return ""


_LOG_TAIL_LIMIT_BYTES = 256 * 1024


def _read_log_tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _LOG_TAIL_LIMIT_BYTES))
            return handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _crest_topology_change_explanation(stdout_text: str) -> str:
    lowered = stdout_text.lower()
    if (
        "change in topology detected" not in lowered
        and "a topology change was seen in the initial geometry optimization" not in lowered
    ):
        return ""
    affected_atoms = ""
    lines = stdout_text.splitlines()
    for index, line in enumerate(lines):
        if "topology change compared to the input affects atoms:" not in line.lower():
            continue
        for candidate in lines[index + 1 :]:
            candidate = candidate.strip()
            if candidate:
                affected_atoms = candidate
                break
        break
    explanation = (
        "CREST stopped because the initial geometry optimization changed molecular topology."
    )
    if affected_atoms:
        explanation += f" Affected atoms: {affected_atoms}."
    return (
        f"{explanation} Check the input geometry; if the change is intentional, "
        "set crest.noreftopo: true before restarting, noting that this can retain artifacts."
    )


def _relative_href(path: Path, workspace_dir: Path) -> str:
    try:
        return os.path.relpath(path, workspace_dir)
    except ValueError:
        return str(path)


def _direct_single_link_file(path: Path, generation_dir: Path) -> Path | None:
    try:
        details = path.stat(follow_symlinks=False)
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if (
        path.parent != generation_dir
        or resolved != path
        or not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
    ):
        return None
    return path


def _stage_diagnostic(
    stage: Mapping[str, Any], workspace_dir: Path, *, include_job_artifacts: bool
) -> tuple[str, str, str | None]:
    if not include_job_artifacts:
        report_path, report = None, None
    else:
        report_path, report = _stage_job_report(stage)
    reason = _stage_status_reason(stage, report)
    if reason == "completed":
        reason = ""
    explanation = _text(_stage_metadata(stage).get("reaction_handoff_message"))
    if not include_job_artifacts:
        return reason, explanation, None
    job_dir = report_path.parent if report_path is not None else None
    details_path: Path | None = None
    engine = _text(_stage_task(stage).get("engine")).lower()
    if engine == "crest" and job_dir is not None:
        stdout_path = job_dir / "crest.stdout.log"
        explanation = _crest_topology_change_explanation(_read_log_tail(stdout_path))
        if explanation:
            details_path = stdout_path
    if engine == "orca" and job_dir is not None and report is not None:
        details_path = _direct_single_link_file(job_dir / RUN_REPORT_HTML_FILE, job_dir)
        if details_path is None:
            details_path = report_path
    if details_path is None and job_dir is not None:
        artifact_names = (
            ("job_state.json",) if engine in {"xtb", "crest"} else (RUN_REPORT_JSON_FILE,)
        )
        for name in artifact_names:
            candidate = job_dir / name
            if candidate.exists():
                details_path = candidate
                break
    details_href = _relative_href(details_path, workspace_dir) if details_path is not None else None
    return reason, explanation, details_href


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
    final_out_path = _text(final_result.get("last_out_path"))
    candidates = [final_out_path]
    attempts = engine_payload.get("attempts")
    if isinstance(attempts, list):
        for attempt in reversed(attempts[-(_MAX_ORCA_ENERGY_CANDIDATES - 1) :]):
            if isinstance(attempt, Mapping):
                candidates.append(_text(attempt.get("out_path")))

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
    stage_id = _text(stage.get("stage_id"))
    label = _text(metadata.get("selected_input_label")) or stage_id

    reason = ""
    attempt_count = 0
    imaginary_count: int | None = None
    report_json_path, report_payload = _stage_job_report(stage)
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

    energy = None
    if authoritative_evidence is not None:
        block, _selected_input_identity = authoritative_evidence
        energy = block.result.energy_hartree
        imaginary_count = block.imaginary_count
    elif (
        _text(stage.get("status")).lower() != "completed"
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
        job_report_html = report_json_path.parent / RUN_REPORT_HTML_FILE
        if _direct_single_link_file(job_report_html, report_json_path.parent) is not None:
            try:
                report_href = os.path.relpath(job_report_html, workspace_dir)
            except ValueError:
                report_href = str(job_report_html)

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
        status=_text(stage.get("status")),
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
    requested_at = _text(payload.get("requested_at"))
    last_advanced_at = _text(metadata.get("last_advanced_at"))

    stage_rows: list[WorkflowStageRow] = []
    failure_rows: list[WorkflowFailureRow] = []
    orca_results: list[OrcaStageResult] = []
    consumed_orca_machine_paths: list[Path] = []
    crest_total: int | None = None
    xtb_total: int | None = None
    for stage in workflow_stage_dicts(payload):
        stage_kind = _text(stage.get("stage_kind"))
        stage_status = _text(stage.get("status")).lower()
        task = _stage_task(stage)
        task_status = _text(task.get("status")).lower()
        engine = _text(task.get("engine")).lower() or stage_kind.removesuffix("_stage")
        include_job_artifacts = _stage_has_diagnostic_status(stage)
        reason, explanation, details_href = _stage_diagnostic(
            stage,
            workspace_dir,
            include_job_artifacts=include_job_artifacts,
        )
        detail = ""
        if stage_kind == "crest_stage":
            detail, frames = _crest_stage_detail(stage)
            if frames is not None:
                crest_total = (crest_total or 0) + frames
        elif stage_kind == "xtb_stage":
            detail, candidates = _xtb_stage_detail(stage)
            xtb_total = (xtb_total or 0) + candidates
        elif is_orca_stage_kind(stage):
            if is_supported_orca_stage_contract(stage):
                task_kind = _task_kind(stage)
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
                    stage_id=_text(stage.get("stage_id")),
                    engine=engine,
                    status=(stage_status if stage_status in FAILED_STATUSES else task_status),
                    reason=reason,
                    explanation=explanation,
                    details_href=details_href,
                )
            )
        stage_rows.append(
            WorkflowStageRow(
                stage_id=_text(stage.get("stage_id")),
                stage_kind=stage_kind,
                status=stage_status,
                detail=detail,
            )
        )

    workflow_error = _mapping(metadata.get("workflow_error"))

    return WorkflowReportData(
        workflow_id=_text(payload.get("workflow_id")),
        template_name=_text(payload.get("template_name")),
        status=_text(payload.get("status")),
        reaction_key=_text(payload.get("reaction_key")),
        requested_at=requested_at,
        last_advanced_at=last_advanced_at,
        total_duration_text=duration_text(requested_at, last_advanced_at),
        stage_rows=tuple(stage_rows),
        failure_rows=tuple(failure_rows),
        workflow_error_reason=_text(workflow_error.get("reason")),
        workflow_error_message=_text(workflow_error.get("message")),
        workflow_error_scope=_text(workflow_error.get("scope")),
        workflow_error_stage_id=_text(workflow_error.get("stage_id")),
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
    "count_xyz_frames",
    "latest_engrad_energy",
]
