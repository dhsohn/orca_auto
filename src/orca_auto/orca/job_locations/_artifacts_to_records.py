"""Project a directory's ORCA artifacts onto one job-location row.

``normalized_artifact_view`` flattens a schema-version-1 ``machine.json`` /
state payload into the flat keys the index reads. ``record_from_artifacts``
reads the identity, status, selected input, job type, molecule key, resources
and original run directory out of a ``job_state.json`` / ``report.json`` pair
(``report`` wins over ``state``), falling back to the existing row and finally
to what the selected input implies, and builds the row with
``_records.build_job_location_record``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.indexing import JobLocationRecord
from orca_auto.core.indexing.store import normalize_index_text
from orca_auto.core.utils import copy_dict_or_empty, normalize_text

from ..input_artifacts import derive_selected_input_xyz
from ._records import build_job_location_record, resolve_job_metadata
from ._utils import normalize_path_text, resource_dict_from_any


def normalized_artifact_view(source: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    if int(source.get("schema_version", 0) or 0) != 1:
        return dict(source)
    job = copy_dict_or_empty(source.get("job"))
    status = copy_dict_or_empty(source.get("status"))
    input_payload = copy_dict_or_empty(source.get("input"))
    resources = copy_dict_or_empty(source.get("resources"))
    artifacts = copy_dict_or_empty(source.get("artifacts"))
    engine_payload = copy_dict_or_empty(source.get("engine_payload"))
    view = dict(engine_payload)
    view.setdefault("job_id", job.get("id"))
    view.setdefault("queue_id", job.get("queue_id"))
    view.setdefault("job_dir", job.get("dir"))
    view.setdefault("original_run_dir", job.get("dir"))
    view.setdefault("status", status.get("state"))
    view.setdefault("reason", status.get("reason"))
    view.setdefault("selected_input_xyz", input_payload.get("selected_xyz_path"))
    view.setdefault("selected_inp", input_payload.get("primary_path"))
    view.setdefault("manifest_path", artifacts.get("manifest_path"))
    view.setdefault("resource_request", resources.get("request"))
    view.setdefault("resource_actual", resources.get("actual"))
    return view


def first_artifact_value(sources: tuple[dict[str, Any], ...], *keys: str) -> Any:
    for source in sources:
        view = normalized_artifact_view(source)
        for key in keys:
            value = view.get(key)
            if value:
                return value
    return None


def first_artifact_text(sources: tuple[dict[str, Any], ...], *keys: str) -> str:
    value = first_artifact_value(sources, *keys)
    return "" if value is None else normalize_index_text(value)


def _first_resources(
    sources: tuple[dict[str, Any], ...],
    key: str,
    existing: JobLocationRecord | None,
) -> dict[str, int]:
    for source in sources:
        mapped = resource_dict_from_any(normalized_artifact_view(source).get(key))
        if mapped:
            return mapped
    if existing is None:
        return {}
    return dict(getattr(existing, key))


def record_from_artifacts(
    *,
    job_dir: Path,
    state: dict[str, Any] | None,
    report: dict[str, Any] | None,
    existing: JobLocationRecord | None = None,
    fallback_job_id: str = "",
    default_job_type: str = "other",
) -> JobLocationRecord | None:
    sources = (report or {}, state or {})
    job_id = (
        first_artifact_text(sources, "job_id")
        or normalize_text(fallback_job_id)
        or normalize_text(existing.job_id if existing else "")
        or first_artifact_text(sources, "run_id")
    )
    status = first_artifact_text(sources, "status") or "unknown"
    selected_inp = normalize_path_text(first_artifact_value(sources, "selected_inp"))
    selected_input_xyz = normalize_path_text(first_artifact_value(sources, "selected_input_xyz"))
    if not selected_input_xyz.lower().endswith(".xyz"):
        selected_input_xyz = derive_selected_input_xyz(selected_inp)
    selected_input_xyz = (
        selected_input_xyz or selected_inp or (existing.selected_input_xyz if existing else "")
    )
    if not job_id:
        return None

    derived_job_type, derived_molecule_key = resolve_job_metadata(selected_inp, job_dir)
    job_type = (
        normalize_text(
            first_artifact_value(sources, "job_type") or derived_job_type or default_job_type
        )
        or default_job_type
    )
    molecule_key = normalize_text(
        first_artifact_value(sources, "molecule_key")
        or (existing.molecule_key if existing else "")
        or derived_molecule_key
    )
    resource_request = _first_resources(sources, "resource_request", existing)
    resource_actual = _first_resources(sources, "resource_actual", existing)
    original_run_dir = (
        first_artifact_text(sources, "original_run_dir")
        or normalize_text(existing.original_run_dir if existing else "")
        or str(job_dir)
    )
    return build_job_location_record(
        existing=existing,
        job_id=job_id,
        status=status,
        job_dir=Path(original_run_dir),
        job_type=job_type,
        selected_input_xyz=selected_input_xyz,
        molecule_key=molecule_key,
        resource_request=resource_request,
        resource_actual=resource_actual,
    )
