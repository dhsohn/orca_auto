"""Project a directory's ORCA artifacts onto one job-location row.

``record_from_artifacts`` reads the identity, status, selected input, job
type, molecule key, resources and original run directory out of a
``job_state.json`` / ``report.json`` pair (``report`` wins over ``state``),
falling back to the existing row and finally to what the selected input
implies, and hands the parts to ``_records.build_job_location_record``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.indexing import JobLocationRecord

from . import _artifact_records
from ._records import build_job_location_record, resolve_job_metadata
from ._utils import (
    derive_selected_input_xyz,
    normalize_path_text,
    normalize_text,
    resource_dict_from_any,
)


@dataclass(frozen=True)
class _ArtifactRecordPayloads:
    state: dict[str, Any]
    report: dict[str, Any]


@dataclass(frozen=True)
class _ArtifactRecordParts:
    job_id: str
    status: str
    selected_input_xyz: str
    job_type: str
    molecule_key: str
    resource_request: dict[str, int]
    resource_actual: dict[str, int]
    original_run_dir: str


def _artifact_payloads(
    state: dict[str, Any] | None,
    report: dict[str, Any] | None,
) -> _ArtifactRecordPayloads:
    return _ArtifactRecordPayloads(
        state=state or {},
        report=report or {},
    )


def _artifact_record_identity(
    *,
    state: dict[str, Any],
    report: dict[str, Any],
    existing: JobLocationRecord | None,
    fallback_job_id: str,
) -> tuple[str, str, str]:
    sources = (report, state)
    job_id = (
        _artifact_records.first_artifact_text(sources, "job_id")
        or normalize_text(fallback_job_id)
        or normalize_text(existing.job_id if existing else "")
        or _artifact_records.first_artifact_text(sources, "run_id")
    )
    status = _artifact_records.first_artifact_text(sources, "status") or "unknown"
    selected_inp = normalize_path_text(
        _artifact_records.first_artifact_value((report, state), "selected_inp")
    )
    selected_input_xyz = normalize_path_text(
        _artifact_records.first_artifact_value(
            (report, state),
            "selected_input_xyz",
        )
    )
    if not selected_input_xyz.lower().endswith(".xyz"):
        selected_input_xyz = derive_selected_input_xyz(selected_inp)
    selected_input_xyz = (
        selected_input_xyz or selected_inp or (existing.selected_input_xyz if existing else "")
    )
    return job_id, status, selected_input_xyz


def _artifact_job_metadata(
    *,
    job_dir: Path,
    selected_input_xyz: str,
    state: dict[str, Any],
    report: dict[str, Any],
    existing: JobLocationRecord | None,
    default_job_type: str,
) -> tuple[str, str]:
    derived_job_type, derived_molecule_key = resolve_job_metadata(selected_input_xyz, job_dir)
    sources = (report, state)
    job_type = (
        normalize_text(
            _artifact_records.first_artifact_value(sources, "job_type")
            or derived_job_type
            or default_job_type
        )
        or default_job_type
    )
    molecule_key = normalize_text(
        _artifact_records.first_artifact_value(sources, "molecule_key")
        or (existing.molecule_key if existing else "")
        or derived_molecule_key
    )
    return job_type, molecule_key


def _artifact_resources(
    *,
    state: dict[str, Any],
    report: dict[str, Any],
    existing: JobLocationRecord | None,
) -> tuple[dict[str, int], dict[str, int]]:
    return _artifact_records.artifact_resources(
        state=state,
        report=report,
        existing=existing,
        resource_mapping_fn=resource_dict_from_any,
    )


def _artifact_dirs(
    *,
    job_dir: Path,
    state: dict[str, Any],
    report: dict[str, Any],
    existing: JobLocationRecord | None,
) -> str:
    sources = (report, state)
    return (
        _artifact_records.first_artifact_text(sources, "original_run_dir")
        or normalize_text(existing.original_run_dir if existing else "")
        or str(job_dir)
    )


def _artifact_record_parts(
    *,
    job_dir: Path,
    payloads: _ArtifactRecordPayloads,
    existing: JobLocationRecord | None,
    fallback_job_id: str,
    default_job_type: str,
) -> _ArtifactRecordParts | None:
    job_id, status, selected_input_xyz = _artifact_record_identity(
        state=payloads.state,
        report=payloads.report,
        existing=existing,
        fallback_job_id=fallback_job_id,
    )
    if not job_id:
        return None

    job_type, molecule_key = _artifact_job_metadata(
        job_dir=job_dir,
        selected_input_xyz=selected_input_xyz,
        state=payloads.state,
        report=payloads.report,
        existing=existing,
        default_job_type=default_job_type,
    )
    resource_request, resource_actual = _artifact_resources(
        state=payloads.state,
        report=payloads.report,
        existing=existing,
    )
    original_run_dir = _artifact_dirs(
        job_dir=job_dir,
        state=payloads.state,
        report=payloads.report,
        existing=existing,
    )
    return _ArtifactRecordParts(
        job_id=job_id,
        status=status,
        selected_input_xyz=selected_input_xyz,
        job_type=job_type,
        molecule_key=molecule_key,
        resource_request=resource_request,
        resource_actual=resource_actual,
        original_run_dir=original_run_dir,
    )


def _record_from_artifact_parts(
    *,
    existing: JobLocationRecord | None,
    parts: _ArtifactRecordParts,
) -> JobLocationRecord:
    return build_job_location_record(
        existing=existing,
        job_id=parts.job_id,
        status=parts.status,
        job_dir=Path(parts.original_run_dir),
        job_type=parts.job_type,
        selected_input_xyz=parts.selected_input_xyz,
        molecule_key=parts.molecule_key,
        resource_request=parts.resource_request,
        resource_actual=parts.resource_actual,
    )


def record_from_artifacts(
    *,
    job_dir: Path,
    state: dict[str, Any] | None,
    report: dict[str, Any] | None,
    existing: JobLocationRecord | None = None,
    fallback_job_id: str = "",
    default_job_type: str = "other",
) -> JobLocationRecord | None:
    payloads = _artifact_payloads(state, report)
    parts = _artifact_record_parts(
        job_dir=job_dir,
        payloads=payloads,
        existing=existing,
        fallback_job_id=fallback_job_id,
        default_job_type=default_job_type,
    )
    if parts is None:
        return None
    return _record_from_artifact_parts(
        existing=existing,
        parts=parts,
    )
