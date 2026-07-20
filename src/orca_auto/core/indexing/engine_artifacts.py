from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .location import JobLocationRecord
from .text import normalize_index_text as normalize_text


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


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


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


def _existing_artifact_record(
    existing: JobLocationRecord | None,
    *,
    use_existing_fallback: bool,
) -> JobLocationRecord | None:
    return existing if use_existing_fallback else None


def _snapshot_job_id(
    sources: tuple[dict[str, Any], ...],
    existing: JobLocationRecord | None,
) -> str:
    return normalize_text(_first_value(sources, "job_id") or (existing.job_id if existing else ""))


def _snapshot_status(
    sources: tuple[dict[str, Any], ...],
    existing: JobLocationRecord | None,
) -> str:
    return (
        normalize_text(
            _first_value(sources, "status") or (existing.status if existing else "") or "unknown"
        )
        or "unknown"
    )


def _snapshot_payload_kind(
    sources: tuple[dict[str, Any], ...],
    *,
    spec: Any,
    default: str,
) -> str:
    return normalize_text(_first_value(sources, spec.payload_kind_key) or default) or default


def _snapshot_detail_text(
    sources: tuple[dict[str, Any], ...],
    key: str,
    *,
    existing: JobLocationRecord | None,
    existing_value: str,
) -> str:
    return normalize_text(_first_value(sources, key) or (existing_value if existing else ""))


def _snapshot_original_run_dir(
    sources: tuple[dict[str, Any], ...],
    *,
    existing: JobLocationRecord | None,
    job_dir: Path,
) -> str:
    return normalize_text(
        _first_value(sources, "original_run_dir")
        or (existing.original_run_dir if existing else "")
        or str(job_dir)
    )


def _snapshot_molecule_key(
    sources: tuple[dict[str, Any], ...],
    *,
    spec: Any,
    existing: JobLocationRecord | None,
    original_run_dir: str,
    selected_input_xyz: str,
) -> str:
    molecule_key = _snapshot_detail_text(
        sources,
        spec.molecule_key_name,
        existing=existing,
        existing_value=existing.molecule_key if existing else "",
    )
    if molecule_key:
        return molecule_key
    return spec.default_molecule_key(Path(original_run_dir), selected_input_xyz)


@dataclass(frozen=True)
class EngineArtifactSnapshotRequest:
    spec: Any
    job_dir: Path
    state: dict[str, Any]
    report: dict[str, Any]
    job_status_sources: tuple[dict[str, Any], ...]
    detail_sources: tuple[dict[str, Any], ...]
    use_existing_fallback: bool
    existing: JobLocationRecord | None = None
    fallback_payload_kind: str | None = None


@dataclass(frozen=True)
class EngineArtifactSnapshot:
    job_id: str
    status: str
    payload_kind: str
    selected_input_xyz: str
    molecule_key: str
    original_run_dir: str
    resource_request: dict[str, int]
    resource_actual: dict[str, int]

    @classmethod
    def empty(cls) -> EngineArtifactSnapshot:
        return cls(
            job_id="",
            status="",
            payload_kind="",
            selected_input_xyz="",
            molecule_key="",
            original_run_dir="",
            resource_request={},
            resource_actual={},
        )

    @classmethod
    def from_request(cls, request: EngineArtifactSnapshotRequest) -> EngineArtifactSnapshot:
        existing_record = _existing_artifact_record(
            request.existing,
            use_existing_fallback=request.use_existing_fallback,
        )
        job_id = _snapshot_job_id(request.job_status_sources, existing_record)
        if not job_id:
            return cls.empty()

        payload_kind_default = request.fallback_payload_kind or request.spec.payload_kind_default
        selected_input_xyz = _snapshot_detail_text(
            request.detail_sources,
            "selected_input_xyz",
            existing=existing_record,
            existing_value=existing_record.selected_input_xyz if existing_record else "",
        )
        original_run_dir = _snapshot_original_run_dir(
            request.detail_sources,
            existing=existing_record,
            job_dir=request.job_dir,
        )
        molecule_key = _snapshot_molecule_key(
            request.detail_sources,
            spec=request.spec,
            existing=existing_record,
            original_run_dir=original_run_dir,
            selected_input_xyz=selected_input_xyz,
        )
        resource_request, resource_actual = artifact_resources(
            state=request.state,
            report=request.report,
            existing=existing_record,
        )
        return cls(
            job_id=job_id,
            status=_snapshot_status(request.job_status_sources, existing_record),
            payload_kind=_snapshot_payload_kind(
                request.detail_sources,
                spec=request.spec,
                default=payload_kind_default,
            ),
            selected_input_xyz=selected_input_xyz,
            molecule_key=molecule_key,
            original_run_dir=original_run_dir,
            resource_request=resource_request,
            resource_actual=resource_actual,
        )


def _first_value(sources: tuple[dict[str, Any], ...], key: str) -> Any:
    return first_artifact_value(sources, key)


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


def engine_record_from_artifacts(
    *,
    spec: Any,
    build_record_fn: Callable[..., JobLocationRecord],
    job_dir: Path,
    state: dict[str, Any] | None,
    report: dict[str, Any] | None,
    existing: JobLocationRecord | None = None,
    default_payload_kind: str | None = None,
) -> JobLocationRecord | None:
    state = state or {}
    report = report or {}
    fallback_payload_kind = default_payload_kind or spec.payload_kind_default

    snapshot = EngineArtifactSnapshot.from_request(
        EngineArtifactSnapshotRequest(
            spec=spec,
            job_dir=job_dir,
            state=state,
            report=report,
            existing=existing,
            fallback_payload_kind=fallback_payload_kind,
            job_status_sources=(report, state),
            detail_sources=(report, state),
            use_existing_fallback=True,
        )
    )
    if not snapshot.job_id:
        return None

    return build_record_fn(
        existing=existing,
        job_id=snapshot.job_id,
        status=snapshot.status,
        job_dir=Path(snapshot.original_run_dir),
        payload_kind=snapshot.payload_kind,
        selected_input_xyz=snapshot.selected_input_xyz,
        molecule_key=snapshot.molecule_key,
        resource_request=snapshot.resource_request,
        resource_actual=snapshot.resource_actual,
    )
