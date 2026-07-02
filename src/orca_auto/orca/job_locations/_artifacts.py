from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.indexing import JobLocationRecord, resolve_job_location

from ..state import load_organized_ref, load_report_json, load_state
from ._models import JobArtifactContext
from ._records import list_job_location_records, resolve_record_job_dir
from ._tracking import TrackedJobDirDeps
from ._tracking import matching_tracked_job_dirs as _matching_tracked_job_dirs
from ._utils import normalize_text, resolve_existing_job_dir


def record_matches_job_dir(record: JobLocationRecord, job_dir: Path) -> bool:
    resolved_job_dir = job_dir.expanduser().resolve()
    for value in (record.latest_known_path, record.organized_output_dir, record.original_run_dir):
        raw = normalize_text(value)
        if not raw:
            continue
        try:
            resolved = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if resolved == resolved_job_dir:
            return True
    return False


def first_artifact_context(index_root: str | Path, targets: tuple[str, ...]) -> JobArtifactContext:
    for raw_target in targets:
        target = normalize_text(raw_target)
        if not target:
            continue
        context = load_job_artifact_context(index_root, target)
        if context.job_dir is not None:
            return context
    return JobArtifactContext()


def hydrated_organized_ref(context: JobArtifactContext) -> dict[str, Any] | None:
    payload = dict(context.organized_ref) if isinstance(context.organized_ref, dict) else None
    if payload:
        return payload
    if context.record is None:
        return payload
    original_dir = resolve_existing_job_dir(context.record.original_run_dir)
    if original_dir is None or original_dir == context.job_dir:
        return payload
    return load_organized_ref(original_dir)


def job_artifact_context(
    *,
    record: JobLocationRecord | None,
    job_dir: Path | None,
    state: dict[str, Any] | None,
    report: dict[str, Any] | None,
    organized_ref: dict[str, Any] | None,
) -> JobArtifactContext:
    return JobArtifactContext(
        record=record,
        job_dir=job_dir,
        state=dict(state) if isinstance(state, dict) else None,
        report=dict(report) if isinstance(report, dict) else None,
        organized_ref=dict(organized_ref) if isinstance(organized_ref, dict) else None,
    )


def record_organized_dir(record: JobLocationRecord | None) -> Path | None:
    if record is None:
        return None
    organized_candidate = resolve_existing_job_dir(record.organized_output_dir)
    if organized_candidate is not None and organized_candidate.is_dir():
        return organized_candidate
    latest_known_candidate = resolve_existing_job_dir(record.latest_known_path)
    original_candidate = resolve_existing_job_dir(record.original_run_dir)
    if (
        latest_known_candidate is not None
        and latest_known_candidate.is_dir()
        and latest_known_candidate != original_candidate
    ):
        return latest_known_candidate
    return None


def organized_job_dir(job_dir: Path) -> Path | None:
    organized_ref = load_organized_ref(job_dir)
    if not organized_ref:
        return None
    organized_dir = resolve_existing_job_dir(organized_ref.get("organized_output_dir"))
    if organized_dir is None or not organized_dir.is_dir():
        return None
    return organized_dir


def matching_tracked_job_dirs(index_root: str | Path, target: str) -> list[Path]:
    return _matching_tracked_job_dirs(
        index_root,
        target,
        deps=TrackedJobDirDeps(
            normalize_text=normalize_text,
            list_job_location_records=list_job_location_records,
            resolve_record_job_dir=resolve_record_job_dir,
            load_state=load_state,
            load_report_json=load_report_json,
            load_organized_ref=load_organized_ref,
            resolve_existing_job_dir=resolve_existing_job_dir,
        ),
    )


def job_dir_candidates(index_root: str | Path, target: str) -> list[Path]:
    record = resolve_job_location(index_root, target)
    raw_candidates: list[Any] = []
    if record is not None:
        raw_candidates.extend(
            [record.latest_known_path, record.organized_output_dir, record.original_run_dir]
        )
    raw_candidates.append(target)

    candidates: list[Path] = []
    seen: set[Path] = set()
    for value in raw_candidates:
        candidate = resolve_existing_job_dir(value)
        if candidate is None or not candidate.is_dir():
            continue
        for resolved in (organized_job_dir(candidate), candidate):
            if resolved is None or resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(resolved)

    for candidate in matching_tracked_job_dirs(index_root, target):
        if candidate in seen:
            continue
        seen.add(candidate)
        candidates.append(candidate)
    return candidates


def resolve_latest_job_dir(index_root: str | Path, target: str) -> Path | None:
    candidates = job_dir_candidates(index_root, target)
    return candidates[0] if candidates else None


def record_for_job_dir(
    index_root: str | Path, target: str, job_dir: Path
) -> JobLocationRecord | None:
    record = resolve_job_location(index_root, target)
    if record is not None:
        return record
    for candidate_record in list_job_location_records(index_root):
        if record_matches_job_dir(candidate_record, job_dir):
            return candidate_record
    return None


def first_state_report(
    candidates: list[Path],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    state_payload: dict[str, Any] | None = None
    report_payload: dict[str, Any] | None = None
    for job_dir in candidates:
        if state_payload is None:
            state = load_state(job_dir)
            state_payload = dict(state) if state is not None else None
        if report_payload is None:
            report_payload = load_report_json(job_dir)
        if state_payload is not None and report_payload is not None:
            break
    return state_payload, report_payload


def organized_ref_for_primary_dir(
    record: JobLocationRecord | None, primary_dir: Path
) -> dict[str, Any] | None:
    organized_ref_payload = load_organized_ref(primary_dir)
    if organized_ref_payload or record is None:
        return organized_ref_payload
    original_dir = resolve_existing_job_dir(record.original_run_dir)
    if original_dir is None or original_dir == primary_dir:
        return organized_ref_payload
    return load_organized_ref(original_dir)


def load_job_artifact_context(
    index_root: str | Path,
    target: str,
) -> JobArtifactContext:
    candidates = job_dir_candidates(index_root, target)
    if not candidates:
        return JobArtifactContext()

    primary_dir = candidates[0]
    record = record_for_job_dir(index_root, target, primary_dir)
    state_payload, report_payload = first_state_report(candidates)
    organized_ref_payload = organized_ref_for_primary_dir(record, primary_dir)

    return JobArtifactContext(
        record=record,
        job_dir=primary_dir,
        state=state_payload,
        report=report_payload,
        organized_ref=organized_ref_payload,
    )


def load_job_artifacts(
    index_root: str | Path,
    target: str,
) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
    context = load_job_artifact_context(index_root, target)
    return context.job_dir, context.state, context.report
