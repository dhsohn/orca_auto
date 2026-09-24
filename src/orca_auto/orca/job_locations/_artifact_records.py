"""Engine-artifact projection and record assembly for the ORCA job-location index.

``normalized_artifact_view`` flattens a schema-version-1 ``machine.json`` /
state payload into the legacy flat keys the index reads, and
``build_job_location_record`` merges those values with an existing record.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.indexing.location import JobLocationRecord
from orca_auto.core.indexing.store import normalize_index_text as normalize_text
from orca_auto.core.utils import copy_dict_or_empty as _mapping


def resource_mapping(raw: object, *, fallback: dict[str, int] | None = None) -> dict[str, int]:
    if not isinstance(raw, dict):
        return dict(fallback or {})
    result: dict[str, int] = {}
    for key, value in raw.items():
        key_text = normalize_text(key)
        if not key_text:
            continue
        try:
            result[key_text] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def first_artifact_value(sources: tuple[dict[str, Any], ...], *keys: str) -> Any:
    for source in sources:
        view = normalized_artifact_view(source)
        for key in keys:
            value = view.get(key)
            if value:
                return value
    return None


def normalized_artifact_view(source: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    if int(source.get("schema_version", 0) or 0) != 1:
        return dict(source)
    job = _mapping(source.get("job"))
    status = _mapping(source.get("status"))
    input_payload = _mapping(source.get("input"))
    resources = _mapping(source.get("resources"))
    artifacts = _mapping(source.get("artifacts"))
    engine_payload = _mapping(source.get("engine_payload"))
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


def first_artifact_text(sources: tuple[dict[str, Any], ...], *keys: str) -> str:
    value = first_artifact_value(sources, *keys)
    return "" if value is None else normalize_text(value)


def first_resource_mapping(
    sources: tuple[dict[str, Any], ...],
    key: str,
    *,
    existing: JobLocationRecord | None,
    existing_attr: str,
    resource_mapping_fn: Callable[[Any], dict[str, int]],
) -> dict[str, int]:
    for source in sources:
        mapped = resource_mapping_fn(normalized_artifact_view(source).get(key))
        if mapped:
            return mapped
    if existing is None:
        return {}
    return dict(getattr(existing, existing_attr))


def artifact_resources(
    *,
    state: dict[str, Any],
    report: dict[str, Any],
    existing: JobLocationRecord | None,
    resource_mapping_fn: Callable[[Any], dict[str, int]] | None = None,
) -> tuple[dict[str, int], dict[str, int]]:
    mapper = resource_mapping if resource_mapping_fn is None else resource_mapping_fn
    sources = (report, state)
    resource_request = first_resource_mapping(
        sources,
        "resource_request",
        existing=existing,
        existing_attr="resource_request",
        resource_mapping_fn=mapper,
    )
    resource_actual = first_resource_mapping(
        sources,
        "resource_actual",
        existing=existing,
        existing_attr="resource_actual",
        resource_mapping_fn=mapper,
    )
    return resource_request, resource_actual


def resource_dict(max_cores: int, max_memory_gb: int) -> dict[str, int]:
    return {
        "max_cores": max(1, int(max_cores)),
        "max_memory_gb": max(1, int(max_memory_gb)),
    }


def _resolved_existing_path(existing: JobLocationRecord | None, attr: str) -> Path | None:
    value = normalize_text(getattr(existing, attr)) if existing is not None else ""
    return Path(value).expanduser().resolve() if value else None


def _existing_text(existing: JobLocationRecord | None, attr: str) -> str:
    return normalize_text(getattr(existing, attr)) if existing is not None else ""


def _original_run_dir(existing: JobLocationRecord | None, job_dir: Path) -> Path:
    return _resolved_existing_path(existing, "original_run_dir") or job_dir.expanduser().resolve()


def _selected_input_xyz_text(
    existing: JobLocationRecord | None,
    selected_input_xyz: str,
) -> str:
    return normalize_text(selected_input_xyz) or _existing_text(existing, "selected_input_xyz")


def _molecule_key_text(
    existing: JobLocationRecord | None,
    molecule_key: str,
    *,
    original_run_dir: Path,
    selected_input_xyz: str,
    default_molecule_key_fn: Callable[[Path, str], str] | None,
) -> str:
    resolved = normalize_text(molecule_key) or _existing_text(existing, "molecule_key")
    if resolved or default_molecule_key_fn is None:
        return resolved
    return default_molecule_key_fn(original_run_dir, selected_input_xyz)


def _resource_payload(
    provided: dict[str, int] | None,
    existing: JobLocationRecord | None,
    attr: str,
) -> dict[str, int]:
    existing_payload = dict(getattr(existing, attr)) if existing is not None else {}
    return dict(provided or existing_payload)


def build_job_location_record(
    *,
    existing: JobLocationRecord | None = None,
    job_id: str,
    app_name: str,
    job_type: str,
    status: str,
    job_dir: Path,
    selected_input_xyz: str,
    molecule_key: str = "",
    resource_request: dict[str, int] | None = None,
    resource_actual: dict[str, int] | None = None,
    default_molecule_key_fn: Callable[[Path, str], str] | None = None,
) -> JobLocationRecord:
    resolved_job_dir = job_dir.expanduser().resolve()
    original_run_dir = _original_run_dir(existing, resolved_job_dir)
    selected_input_xyz_text = _selected_input_xyz_text(existing, selected_input_xyz)
    molecule_key_text = _molecule_key_text(
        existing,
        molecule_key,
        original_run_dir=original_run_dir,
        selected_input_xyz=selected_input_xyz_text,
        default_molecule_key_fn=default_molecule_key_fn,
    )
    resource_request_text = _resource_payload(
        resource_request,
        existing,
        "resource_request",
    )
    resource_actual_text = (
        _resource_payload(
            resource_actual,
            existing,
            "resource_actual",
        )
        or resource_request_text
    )

    return JobLocationRecord(
        job_id=normalize_text(job_id),
        app_name=app_name,
        job_type=job_type,
        status=normalize_text(status),
        original_run_dir=str(original_run_dir),
        molecule_key=molecule_key_text,
        selected_input_xyz=selected_input_xyz_text,
        latest_known_path=str(resolved_job_dir.resolve()),
        resource_request=resource_request_text,
        resource_actual=resource_actual_text,
    )


__all__ = [
    "artifact_resources",
    "build_job_location_record",
    "first_artifact_text",
    "first_artifact_value",
    "first_resource_mapping",
    "normalized_artifact_view",
    "resource_dict",
    "resource_mapping",
]
