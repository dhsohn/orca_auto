from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orca_auto.core.utils import copy_dict_or_empty as _mapping

from .location import JobLocationRecord
from .store import normalize_index_text as normalize_text


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
