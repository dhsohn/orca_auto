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
import math
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import (
    RUN_REPORT_HTML_FILE,
    RUN_REPORT_JSON_FILE,
    WORKFLOW_REPORT_HTML_FILE,
)
from orca_auto.core.engine_process import read_confined_text
from orca_auto.core.queue.generation import (
    is_visible_generation_name,
    visible_generation_children,
)
from orca_auto.core.utils.persistence import atomic_write_text
from orca_auto.orca.parser import KCAL_PER_HARTREE
from orca_auto.orca.parser.patterns import (
    FINAL_SINGLE_POINT_ENERGY_BYTES_RE,
    final_single_point_energy_value,
)
from orca_auto.orca.report.attempts import duration_text
from orca_auto.orca.report.render import (
    ReportPage,
    metric_card,
    render_page,
    status_badge_kind,
)
from orca_auto.orca.state import load_report_json

logger = logging.getLogger(__name__)

_ENGRAD_ENERGY_MARKER = "current total energy"
_MAX_ENGRAD_ENERGY_FILE_BYTES = 8 * 1024 * 1024
_MAX_ORCA_ENERGY_SCAN_BYTES = 256 * 1024
_MAX_ORCA_ENERGY_CANDIDATES = 8
_ORCA_ENERGY_READ_CHUNK_BYTES = 64 * 1024
_FAILED_STAGE_STATUSES = frozenset({"failed", "cancel_failed", "submission_failed"})
_DIAGNOSTIC_STAGE_STATUSES = frozenset({*_FAILED_STAGE_STATUSES, "cancelled"})
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


def _stage_dicts(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    stages = payload.get("stages")
    if not isinstance(stages, list):
        return []
    return [stage for stage in stages if isinstance(stage, dict)]


def _stage_metadata(stage: Mapping[str, Any]) -> dict[str, Any]:
    metadata = stage.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _stage_task(stage: Mapping[str, Any]) -> dict[str, Any]:
    task = stage.get("task")
    return task if isinstance(task, dict) else {}


def _stage_task_payload(stage: Mapping[str, Any]) -> dict[str, Any]:
    payload = _stage_task(stage).get("payload")
    return payload if isinstance(payload, dict) else {}


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


_INTERACTION_ROLE_PREFIX = "interaction_"


def _interaction_role_stage(stage: Mapping[str, Any]) -> bool:
    """True for interaction-energy fan-out stages (``role: interaction_*``).

    Fragment and complex single points carry a different stoichiometry or
    level of theory than the conformer/TS candidates, so they are ΔE_int
    inputs only and must never rank in the candidate table or set its ΔE
    baseline.
    """
    return _text(_stage_metadata(stage).get("role")).startswith(_INTERACTION_ROLE_PREFIX)


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
        except (OSError, RuntimeError, UnicodeError, ValueError):
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


def _stage_job_dirs(stage: Mapping[str, Any]) -> tuple[Path, ...]:
    metadata = _stage_metadata(stage)
    task_payload = _stage_task_payload(stage)
    paths: list[Path] = []
    seen: set[str] = set()
    for value in (
        metadata.get("latest_known_path"),
        task_payload.get("job_dir"),
        task_payload.get("reaction_dir"),
    ):
        path_text = _text(value)
        if path_text and path_text not in seen:
            paths.append(Path(path_text))
            seen.add(path_text)
    return tuple(paths)


def _stage_job_report(stage: Mapping[str, Any]) -> tuple[Path | None, dict[str, Any] | None]:
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
                and _stage_report_identity_matches(
                    stage,
                    state,
                    require_job_and_run=False,
                )
            ):
                return state_path, state
        return None, None
    if task_engine in _INTERNAL_STAGE_ENGINES.values():
        return None, None
    for job_dir in _stage_job_dirs(stage):
        candidate_dirs = (
            (job_dir,)
            if is_visible_generation_name(job_dir.name)
            else visible_generation_children(job_dir)
        )
        for candidate_dir in candidate_dirs:
            report_path = candidate_dir / RUN_REPORT_JSON_FILE
            report = _verified_orca_stage_report(stage, report_path)
            if report is not None:
                return report_path, report
    return None, None


def _verified_orca_stage_report(
    stage: Mapping[str, Any],
    report_path: Path | None,
) -> dict[str, Any] | None:
    if (
        report_path is None
        or not report_path.is_absolute()
        or report_path.name != RUN_REPORT_JSON_FILE
        or report_path != report_path.parent / RUN_REPORT_JSON_FILE
        or not is_visible_generation_name(report_path.parent.name)
    ):
        return None
    if report_path.is_symlink() or not report_path.is_file():
        return None
    try:
        if report_path.resolve(strict=True) != report_path:
            return None
    except OSError:
        return None
    report = load_report_json(report_path.parent, require_consumable_success=True)
    if report is None or not _stage_report_identity_matches(stage, report):
        return None
    return report


def _resolve_orca_stage_report(
    stage: Mapping[str, Any],
) -> tuple[Path | None, dict[str, Any] | None]:
    """Resolve the canonical verified report for one ORCA workflow stage."""

    report_path = _stage_artifact_path(stage, "orca_report_json")
    report = _verified_orca_stage_report(stage, report_path)
    if report is not None:
        return report_path, report
    return _stage_job_report(stage)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _identity_values(*values: Any) -> frozenset[str]:
    return frozenset(text for value in values if (text := _text(value)))


def _stage_report_identity_matches(
    stage: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    require_job_and_run: bool = True,
) -> bool:
    metadata = _stage_metadata(stage)
    task = _stage_task(stage)
    task_payload = _stage_task_payload(stage)
    submission = _mapping(task.get("submission_result"))
    job = _mapping(report.get("job"))
    engine_payload = _mapping(report.get("engine_payload"))

    stage_job_ids = _identity_values(
        metadata.get("child_job_id"),
        submission.get("job_id"),
    )
    # Only the nested identities are read: the flat top-level keys belong to a
    # pre-schema artifact layout that the engine-key and schema gates above no
    # longer admit, so accepting them here would only widen the conflict guard
    # against values no writer produces.
    report_job_ids = _identity_values(job.get("id"), job.get("task_id"))
    stage_run_ids = _identity_values(metadata.get("run_id"), task_payload.get("run_id"))
    report_run_ids = _identity_values(engine_payload.get("run_id"))
    stage_queue_ids = _identity_values(metadata.get("queue_id"), submission.get("queue_id"))
    report_queue_ids = _identity_values(job.get("queue_id"))
    identity_pairs = (
        (stage_job_ids, report_job_ids),
        (stage_run_ids, report_run_ids),
        (stage_queue_ids, report_queue_ids),
    )
    if any(
        len(stage_values) > 1 or len(report_values) > 1
        for stage_values, report_values in identity_pairs
    ):
        return False
    if require_job_and_run:
        required_pairs = identity_pairs[:2]
        if any(
            not stage_values or report_values != stage_values
            for stage_values, report_values in required_pairs
        ):
            return False
        return not (stage_queue_ids and report_queue_ids and report_queue_ids != stage_queue_ids)

    declared_pairs = tuple(
        (stage_values, report_values)
        for stage_values, report_values in identity_pairs
        if stage_values
    )
    return bool(declared_pairs) and all(
        report_values == stage_values for stage_values, report_values in declared_pairs
    )


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


def _read_log_tail(path: Path, *, limit_bytes: int = 256 * 1024) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit_bytes))
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
    elif _text(stage.get("stage_kind")) == "orca_stage":
        report_path, report = _resolve_orca_stage_report(stage)
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


def _read_pinned_orca_output_tail(output_root: Path, candidate: Path) -> tuple[bytes, int] | None:
    """Read at most the bounded tail of one confined, stable regular output."""
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

        read_size = min(opened.st_size, _MAX_ORCA_ENERGY_SCAN_BYTES)
        read_offset = opened.st_size - read_size
        tail = bytearray()
        while len(tail) < read_size:
            chunk = os.pread(
                output_fd,
                min(_ORCA_ENERGY_READ_CHUNK_BYTES, read_size - len(tail)),
                read_offset + len(tail),
            )
            if not chunk:
                break
            tail.extend(chunk)

        after = os.fstat(output_fd)
        # A file that grew under the read must be rejected, not parsed. The
        # energy marker's `$` also matches at the end of the tail buffer, so a
        # half-written number would parse as a complete value and be displayed
        # as a wrong energy in the delta-E table.
        if len(tail) != read_size or (opened.st_size, opened.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            return None
        return bytes(tail), read_offset
    except (OSError, ValueError):
        return None
    finally:
        if output_fd >= 0:
            os.close(output_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _final_single_point_energy_from_output(output_root: Path, candidate: Path) -> float | None:
    tail_snapshot = _read_pinned_orca_output_tail(output_root, candidate)
    if tail_snapshot is None:
        return None
    tail, read_offset = tail_snapshot

    energy_text: bytes | None = None
    for match in FINAL_SINGLE_POINT_ENERGY_BYTES_RE.finditer(tail):
        # A bounded tail can start in the middle of a line.  Do not treat that
        # truncated first line as a complete ORCA marker.
        if read_offset and match.start() == 0:
            continue
        if match.group(2) is not None:
            # Annotated values ("(SCF not fully converged!)") never feed the
            # report's fallback energy, matching this site's prior pattern.
            continue
        energy_text = match.group(1)
    if energy_text is None:
        return None
    try:
        energy = final_single_point_energy_value(energy_text)
    except ValueError:
        return None
    return energy if math.isfinite(energy) else None


def _orca_report_output_energy(
    output_dir: Path,
    report_payload: Mapping[str, Any],
) -> float | None:
    """Final bounded ORCA output energy when a stage did not retain an ``.engrad``."""
    try:
        output_root = output_dir.resolve(strict=True)
    except OSError:
        return None

    engine_payload = _mapping(report_payload.get("engine_payload"))
    final_result = _mapping(engine_payload.get("final_result"))
    candidates = [_text(final_result.get("last_out_path"))]
    attempts = engine_payload.get("attempts")
    if isinstance(attempts, list):
        for attempt in reversed(attempts[-(_MAX_ORCA_ENERGY_CANDIDATES - 1) :]):
            if isinstance(attempt, Mapping):
                candidates.append(_text(attempt.get("out_path")))

    seen: set[str] = set()
    for raw_path in candidates:
        if not raw_path:
            continue
        candidate_key = os.path.abspath(raw_path)
        if candidate_key in seen:
            continue
        seen.add(candidate_key)
        energy = _final_single_point_energy_from_output(output_root, Path(raw_path))
        if energy is not None:
            return energy
    return None


def _orca_stage_result(stage: Mapping[str, Any], workspace_dir: Path) -> OrcaStageResult:
    metadata = _stage_metadata(stage)
    stage_id = _text(stage.get("stage_id"))
    label = _text(metadata.get("selected_input_label")) or stage_id

    reason = ""
    attempt_count = 0
    imaginary_count: int | None = None
    report_json_path, report_payload = _resolve_orca_stage_report(stage)
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
    if report_json_path is not None and report_payload is not None:
        # A reusable job root can retain pre-generation ``*.engrad`` files.
        # Both energy sources must therefore be confined to the generation
        # whose report provenance and workflow-stage identity were verified.
        generation_dir = report_json_path.parent
        energy = latest_engrad_energy(generation_dir)
        if energy is None:
            energy = _orca_report_output_energy(generation_dir, report_payload)

    report_href: str | None = None
    if report_json_path is not None:
        # The HTML report is co-located with the generation-local report JSON.
        job_report_html = report_json_path.parent / RUN_REPORT_HTML_FILE
        if _direct_single_link_file(job_report_html, report_json_path.parent) is not None:
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
        machine_path=report_json_path if report_payload is not None else None,
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
    failure_rows: list[WorkflowFailureRow] = []
    orca_results: list[OrcaStageResult] = []
    consumed_orca_machine_paths: list[Path] = []
    crest_total: int | None = None
    xtb_total: int | None = None
    for stage in _stage_dicts(payload):
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
        elif stage_kind == "orca_stage":
            result = _orca_stage_result(stage, workspace_dir)
            if result.machine_path is not None:
                consumed_orca_machine_paths.append(result.machine_path)
            # A relaxed scan is a prerequisite, not a TS candidate: keep it in
            # the stage chain but out of the ranked candidate table so its
            # non-stationary energy never sets the ΔE baseline. Interaction
            # fan-out stages stay out for the same reason — their energies
            # belong to the ΔE_int table, not the candidate ranking.
            if _task_kind(stage) != "relaxed_scan" and not _interaction_role_stage(stage):
                orca_results.append(result)
            detail_parts = [part for part in (result.label, result.reason) if part]
            detail = " · ".join(detail_parts)
        diagnostic_detail = explanation or reason
        if diagnostic_detail and diagnostic_detail not in detail:
            detail = " · ".join(part for part in (detail, diagnostic_detail) if part)
        if stage_status in _FAILED_STAGE_STATUSES or task_status in _FAILED_STAGE_STATUSES:
            failure_rows.append(
                WorkflowFailureRow(
                    stage_id=_text(stage.get("stage_id")),
                    engine=engine,
                    status=(
                        stage_status if stage_status in _FAILED_STAGE_STATUSES else task_status
                    ),
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
    if data.status not in {"failed", "cancel_failed", "submission_failed"}:
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
    "OrcaStageResult",
    "WorkflowReportData",
    "WorkflowFailureRow",
    "WorkflowStageRow",
    "collect_workflow_report_data",
    "count_xyz_frames",
    "latest_engrad_energy",
    "render_workflow_report_html",
    "write_workflow_html_report",
]
